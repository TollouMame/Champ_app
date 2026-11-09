"""
API FastAPI exposant les fonctions déjà testées (auth.py, fields.py, main.py).

Lancer en local :
    uvicorn src.api:app --reload --port 8000   (depuis la racine du projet champ_app/)

Documentation interactive générée automatiquement : http://localhost:8000/docs
"""

import os
import time
import uuid
from datetime import date
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from src import tokens as tokens_module
from src.db import get_connection, init_db
from src.auth import register_user, authenticate_user, AuthError, get_user
from src.analyses import save_analysis, list_analyses, count_analyses
from src.treatments import save_treatment, list_treatments, TreatmentError
from src.fields import register_field, list_fields, get_field, FieldError, CROPS_DISPONIBLES
from src.config import load_environment
from src.demo_data import generate_synthetic_bands, synthetic_size_for_bbox
from src.fetch_sentinel import SentinelHubConfigError, fetch_field_bands
from src.geometry import LOW_QUALITY_PIXEL_RATIO, MIN_VALID_PIXEL_RATIO, prepare_bands
from src.privacy import delete_field, delete_user_account, export_user_data, pseudonymize_email
from src.logging_config import setup_logging, get_logger
from src.main import run_pipeline
from src.zoning import get_thresholds
from src.tokens import is_development_environment

load_environment()
setup_logging()
logger = get_logger("champ_app")

DB_PATH = Path(__file__).resolve().parent.parent / "output" / "app.db"


def get_db_path() -> Path:
    env_path = os.environ.get("CHAMP_DB_PATH")
    if env_path:
        return Path(env_path)
    return DB_PATH


def get_allowed_origins() -> list[str]:
    raw_value = os.environ.get("CORS_ALLOWED_ORIGINS")
    if raw_value:
        return [item.strip() for item in raw_value.split(",") if item.strip()]

    return [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]


def get_auth_rate_limit() -> str:
    """Limite de requêtes sur les routes d'authentification (protection brute-force).
    Format slowapi, ex: '10/minute'. Configurable via AUTH_RATE_LIMIT."""
    return os.environ.get("AUTH_RATE_LIMIT", "10/minute")


def get_export_rate_limit() -> str:
    """Limite d'export de données (coûteux : relit toutes les analyses du compte)."""
    return os.environ.get("EXPORT_RATE_LIMIT", "10/hour")


def get_analysis_rate_limit() -> str:
    """Limite de création d'analyses (coût CPU et quota Sentinel Hub). Format slowapi."""
    return os.environ.get("ANALYSIS_RATE_LIMIT", "60/hour")


def get_max_body_bytes() -> int:
    return int(os.environ.get("MAX_BODY_BYTES", 1_000_000))


# Nombre maximal d'analyses conservées par champ (stockage et coût Sentinel).
MAX_ANALYSES_PER_FIELD = 200
MIN_RESOLUTION_M = 10   # résolution native de Sentinel-2 : en dessous, on ne gagne rien et on coûte cher

limiter = Limiter(key_func=get_remote_address)


async def _handle_rate_limit_exceeded(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    detail = "Trop de tentatives, réessayez plus tard."
    return JSONResponse(status_code=429, content={"error": "rate_limited", "detail": detail, "message": detail})


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = get_db_path()
    db_path.parent.mkdir(exist_ok=True)
    # échoue dès le démarrage si JWT_SECRET_KEY est absent hors ENV=development
    tokens_module.get_secret_key()
    init_db(db_path)
    yield


def get_docs_kwargs() -> dict:
    """/docs, /redoc et /openapi.json décrivent toute l'API à un attaquant : ils ne sont
    servis qu'avec ENV=development explicite (même logique que le secret JWT)."""
    if is_development_environment():
        return {}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


app = FastAPI(title="Suivi de champs — API", lifespan=lifespan, **get_docs_kwargs())
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _handle_rate_limit_exceeded)


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    """Refuse (413) une requête dont Content-Length dépasse MAX_BODY_BYTES (1 Mo par défaut).

    Ne couvre pas les corps envoyés en chunked sans Content-Length : poser aussi
    la limite sur le reverse proxy (ex. client_max_body_size 1m avec nginx).
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > get_max_body_bytes():
        detail = "Requête trop volumineuse."
        return JSONResponse(
            status_code=413,
            content={"detail": {"error": "payload_too_large", "detail": detail, "message": detail}},
        )
    return await call_next(request)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Journalise chaque requête (méthode, chemin, statut, durée) avec un
    request_id unique, propagé aussi dans l'en-tête de réponse X-Request-ID
    pour pouvoir corréler un incident signalé par un utilisateur avec les logs."""
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    start = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.exception(
            "requête non gérée (exception)",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
            },
        )
        raise

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    log = logger.warning if response.status_code >= 400 else logger.info
    log(
        "requête terminée",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    response.headers["X-Request-ID"] = request_id
    return response


# Politique de sécurité du contenu des pages (HTML). Les scripts et styles inline des
# pages imposent 'unsafe-inline', mais le plus important est conservé :
# connect-src 'self' empêche d'envoyer le token à un serveur tiers même si du code
# injecté s'exécutait, et script-src liste les seules origines autorisées.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https://*.tile.openstreetmap.org",
    "connect-src 'self'",
    "worker-src 'self'",
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


def _csp_header_name() -> str:
    """CSP_REPORT_ONLY=1 : le navigateur signale les violations sans rien bloquer (utile
    pour tester une nouvelle politique sur de vrais appareils avant de l'appliquer)."""
    report_only = os.environ.get("CSP_REPORT_ONLY", "").strip().lower() in {"1", "true", "yes"}
    return "Content-Security-Policy-Report-Only" if report_only else "Content-Security-Policy"


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    headers.setdefault("Permissions-Policy", "geolocation=(self), camera=(), microphone=(), payment=()")
    if not is_development_environment():
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # la CSP ne vise que les pages ; la documentation Swagger (dev) charge ses scripts depuis un CDN
    is_html = response.headers.get("content-type", "").startswith("text/html")
    if is_html and request.url.path not in ("/docs", "/redoc"):
        headers.setdefault(_csp_header_name(), CONTENT_SECURITY_POLICY)
    return response


def _raise_api_error(status_code: int, error: str, detail: str) -> None:
    raise HTTPException(status_code=status_code, detail={"error": error, "detail": detail, "message": detail})


def get_current_user_id(authorization: str = Header(None)) -> int:
    """
    Dépendance FastAPI : extrait et vérifie le token JWT de l'en-tête
    'Authorization: Bearer <token>'. Lève 401 si absent ou invalide.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        _raise_api_error(401, "authentication_error", "Token d'authentification manquant.")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        user_id = tokens_module.decode_access_token(token)
    except tokens_module.TokenError as e:
        _raise_api_error(401, "authentication_error", str(e))
    # Un token reste cryptographiquement valide 7 jours : sans ce contrôle, un compte
    # supprimé (droit à l'effacement) continuerait d'être accepté, et créer un champ
    # avec un utilisateur inexistant lèverait une erreur de clé étrangère (500).
    if get_user(get_db_path(), user_id) is None:
        _raise_api_error(401, "authentication_error", "Compte introuvable ou supprimé.")
    return user_id

# Config CORS configurable via variable d'environnement pour éviter l'ouverture totale en production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def api_health():
    """Sonde de disponibilité, publique : volontairement sans détail interne (chemin du
    fichier, environnement). Vérifie réellement que la base répond."""
    try:
        conn = get_connection(get_db_path())
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception:
        logger.exception("sonde de santé : base de données indisponible")
        return JSONResponse(status_code=503, content={"status": "degraded", "database": "unavailable"})
    return {"status": "ok", "database": "ready"}


# ---------- schémas de requête/réponse ----------

class RegisterRequest(BaseModel):
    # Limites larges : elles ne servent qu'à borner la mémoire. Les vraies règles (et leurs
    # messages en français) sont dans auth.py ; avec max_length=128 ici, Pydantic répondait
    # « String should have at most 128 characters » en anglais, affiché sous le champ email.
    email: str = Field(max_length=1024)
    password: str = Field(max_length=1024)


class AuthResponse(BaseModel):
    user_id: int
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int = 7 * 24 * 60 * 60


class FieldCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    crop: str = Field(max_length=100)
    bbox: tuple[float, float, float, float] | None = None  # min_lon, min_lat, max_lon, max_lat
    polygon: list[tuple[float, float]] | None = None  # [(lon, lat), ...], prime sur bbox si fourni
    sowing_date: str | None = Field(default=None, max_length=32)

    @field_validator("crop")
    @classmethod
    def crop_must_be_known(cls, v):
        if v not in CROPS_DISPONIBLES:
            raise ValueError(f"Culture inconnue. Choix possibles : {', '.join(CROPS_DISPONIBLES)}")
        return v


class FieldResponse(BaseModel):
    id: int
    name: str
    crop: str
    sowing_date: str | None
    area_ha: float
    polygon: list[tuple[float, float]] | None = None


class AnalysisRequest(BaseModel):
    source: str = "synthetic"
    acquisition_date: date | None = None
    resolution: int = 10
    method: str = "grid"
    n_rows: int = 8
    n_cols: int = 12
    n_zones: int = 6
    critical_threshold: float | None = None
    warning_threshold: float | None = None


class AccountDeleteRequest(BaseModel):
    password: str = Field(max_length=1024)  # borne mémoire seulement ; la vérification fait le reste


class ZoneTreatmentRequest(BaseModel):
    analysis_id: int
    zone_id: str = Field(max_length=16)
    note: str | None = Field(default=None, max_length=1000)


# ---------- routes ----------

@app.post("/auth/register", response_model=AuthResponse)
@limiter.limit(get_auth_rate_limit)
def api_register(request: Request, payload: RegisterRequest):
    email = payload.email.strip().lower()
    if not email or "@" not in email or "." not in email.split("@", 1)[1]:
        _raise_api_error(422, "validation_error", "Adresse email invalide.")

    try:
        user_id = register_user(get_db_path(), email, payload.password)
    except AuthError as e:
        logger.warning(
            "échec d'inscription",
            extra={"request_id": getattr(request.state, "request_id", None), "email_hash": pseudonymize_email(email), "reason": str(e)},
        )
        _raise_api_error(400, "auth_error", str(e))
    logger.info(
        "inscription réussie",
        extra={"request_id": getattr(request.state, "request_id", None), "user_id": user_id},
    )
    return {
        "user_id": user_id,
        "access_token": tokens_module.create_access_token(user_id),
        "expires_in_seconds": tokens_module.TOKEN_EXPIRY_HOURS * 60 * 60,
    }


@app.post("/auth/login", response_model=AuthResponse)
@limiter.limit(get_auth_rate_limit)
def api_login(request: Request, payload: RegisterRequest):
    try:
        user_id = authenticate_user(get_db_path(), payload.email, payload.password)
    except AuthError as e:
        # utile pour repérer une attaque par force brute dans les logs, sans jamais loguer le mot de passe
        logger.warning(
            "échec de connexion",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "email_hash": pseudonymize_email(payload.email),
                "client_ip": get_remote_address(request),
            },
        )
        _raise_api_error(401, "auth_error", str(e))
    logger.info(
        "connexion réussie",
        extra={"request_id": getattr(request.state, "request_id", None), "user_id": user_id},
    )
    return {
        "user_id": user_id,
        "access_token": tokens_module.create_access_token(user_id),
        "expires_in_seconds": tokens_module.TOKEN_EXPIRY_HOURS * 60 * 60,
    }


@app.get("/account/export")
@limiter.limit(get_export_rate_limit)
def api_export_account(request: Request, user_id: int = Depends(get_current_user_id)):
    """Droit d'accès / portabilité : toutes les données du compte en JSON (sans mot de passe)."""
    data = export_user_data(get_db_path(), user_id)
    logger.info(
        "export des données",
        extra={"request_id": getattr(request.state, "request_id", None), "user_id": user_id, "fields": len(data["fields"])},
    )
    return JSONResponse(
        content=data,
        headers={
            "Content-Disposition": 'attachment; filename="champ_app_mes_donnees.json"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/account/delete")
@limiter.limit(get_auth_rate_limit)
def api_delete_account(
    request: Request, payload: AccountDeleteRequest, user_id: int = Depends(get_current_user_id)
):
    """Droit à l'effacement : supprime définitivement le compte et toutes ses données.

    Exige le mot de passe (un token volé ne suffit pas à détruire un compte) et partage la
    limite de débit des routes d'authentification (sinon ce serait un moyen de tester des
    mots de passe sans limite). POST plutôt que DELETE : certains proxys suppriment le
    corps d'un DELETE.
    """
    user = get_user(get_db_path(), user_id)
    try:
        authenticate_user(get_db_path(), user["email"], payload.password)
    except AuthError:
        logger.warning(
            "suppression de compte refusée : mot de passe incorrect",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "user_id": user_id,
                "client_ip": get_remote_address(request),
            },
        )
        _raise_api_error(403, "invalid_password", "Mot de passe incorrect.")

    deleted = delete_user_account(get_db_path(), user_id)
    logger.info(
        "compte supprimé",
        extra={"request_id": getattr(request.state, "request_id", None), "user_id": user_id, **deleted},
    )
    return {"deleted": True, **deleted}


@app.delete("/fields/{field_id}")
def api_delete_field(field_id: int, user_id: int = Depends(get_current_user_id)):
    """Supprime un champ, ses analyses et ses traitements."""
    deleted = delete_field(get_db_path(), field_id, user_id)
    if deleted is None:
        _raise_api_error(404, "not_found", "Champ introuvable.")
    logger.info("champ supprimé", extra={"user_id": user_id, "field_id": field_id, **deleted})
    return {"deleted": True, "field_id": field_id, **deleted}


@app.post("/fields", response_model=FieldResponse)
def api_create_field(payload: FieldCreateRequest, user_id: int = Depends(get_current_user_id)):
    if payload.bbox is None and payload.polygon is None:
        _raise_api_error(400, "validation_error", "Il faut fournir soit une bbox, soit un polygone.")
    try:
        field_id = register_field(
            get_db_path(), user_id, payload.name, payload.crop,
            bbox=payload.bbox, sowing_date=payload.sowing_date, polygon=payload.polygon,
        )
    except FieldError as e:
        logger.warning("échec de création de champ", extra={"user_id": user_id, "reason": str(e)})
        _raise_api_error(400, "validation_error", str(e))

    logger.info("champ créé", extra={"user_id": user_id, "field_id": field_id, "crop": payload.crop})
    field = get_field(get_db_path(), field_id)
    return field


@app.get("/fields", response_model=list[FieldResponse])
def api_list_fields(user_id: int = Depends(get_current_user_id)):
    return list_fields(get_db_path(), user_id)


@app.get("/fields/{field_id}", response_model=FieldResponse)
def api_get_field(field_id: int, user_id: int = Depends(get_current_user_id)):
    field = get_field(get_db_path(), field_id)
    if field is None:
        _raise_api_error(404, "not_found", "Champ introuvable.")
    if field["user_id"] != user_id:
        _raise_api_error(404, "not_found", "Champ introuvable.")
    return field


def _get_owned_field(field_id: int, user_id: int) -> dict:
    field = get_field(get_db_path(), field_id)
    if field is None or field["user_id"] != user_id:
        _raise_api_error(404, "not_found", "Champ introuvable.")
    return field


@app.post("/fields/{field_id}/analyses")
@limiter.limit(get_analysis_rate_limit)
def api_create_analysis(
    request: Request,
    field_id: int,
    payload: AnalysisRequest,
    user_id: int = Depends(get_current_user_id),
):
    field = _get_owned_field(field_id, user_id)
    if payload.source not in ("synthetic", "sentinel"):
        _raise_api_error(422, "validation_error", "source doit être 'synthetic' ou 'sentinel'.")
    if payload.method not in ("grid", "kmeans"):
        _raise_api_error(422, "validation_error", "method doit être 'grid' ou 'kmeans'.")
    if payload.n_rows < 1 or payload.n_cols < 1 or payload.n_zones < 1:
        _raise_api_error(422, "validation_error", "Les dimensions de zonage doivent être des entiers positifs.")
    if payload.resolution < MIN_RESOLUTION_M:
        _raise_api_error(
            422, "validation_error", f"resolution ne peut pas être inférieure à {MIN_RESOLUTION_M} m (résolution native de Sentinel-2)."
        )
    if payload.n_rows > 50 or payload.n_cols > 50:
        _raise_api_error(422, "validation_error", "n_rows et n_cols ne peuvent pas dépasser 50.")
    if payload.n_zones > 20:
        _raise_api_error(422, "validation_error", "n_zones ne peut pas dépasser 20.")
    if payload.resolution > 100:
        _raise_api_error(422, "validation_error", "resolution ne peut pas dépasser 100.")

    if count_analyses(get_db_path(), field_id) >= MAX_ANALYSES_PER_FIELD:
        _raise_api_error(
            429,
            "analysis_quota_exceeded",
            f"Limite de {MAX_ANALYSES_PER_FIELD} analyses atteinte pour ce champ.",
        )

    thresholds = {
        "critical": payload.critical_threshold,
        "warning": payload.warning_threshold,
    }
    try:
        get_thresholds(thresholds)
    except ValueError as exc:
        _raise_api_error(422, "validation_error", str(exc))
    if payload.source == "synthetic":
        demo_height, demo_width = synthetic_size_for_bbox(
            (field["bbox_min_lon"], field["bbox_min_lat"], field["bbox_max_lon"], field["bbox_max_lat"])
        )
        bands = generate_synthetic_bands(height=demo_height, width=demo_width, seed=field_id)
    else:
        bbox = (
            field["bbox_min_lon"],
            field["bbox_min_lat"],
            field["bbox_max_lon"],
            field["bbox_max_lat"],
        )
        try:
            bands = fetch_field_bands(bbox, date=payload.acquisition_date, resolution=payload.resolution)
        except SentinelHubConfigError as exc:
            logger.error("Sentinel Hub mal configuré", extra={"field_id": field_id, "reason": str(exc)})
            _raise_api_error(503, "sentinel_configuration_error", str(exc))
        except Exception as exc:
            logger.error("échec de récupération Sentinel", extra={"field_id": field_id, "reason": str(exc)})
            # le détail technique reste dans les logs ; le client reçoit un message générique
            _raise_api_error(
                502,
                "sentinel_request_error",
                "Impossible de récupérer l'image satellite pour le moment. Réessayez plus tard ou avec une autre date.",
            )
    field_bbox = (
        field["bbox_min_lon"],
        field["bbox_min_lat"],
        field["bbox_max_lon"],
        field["bbox_max_lat"],
    )
    try:
        # ne garde que l'intérieur du contour réel de la parcelle (et mesure la
        # part de pixels exploitables : hors contour, hors nuages)
        bands, valid_ratio = prepare_bands(bands, field_bbox, field.get("polygon"))
    except ValueError as exc:
        _raise_api_error(422, "analysis_error", str(exc))
    if valid_ratio < MIN_VALID_PIXEL_RATIO:
        _raise_api_error(
            422,
            "analysis_error",
            f"Image inexploitable : seulement {valid_ratio:.0%} de la parcelle est visible "
            "(nuages, ombres ou absence de données). Essayez une autre date.",
        )
    try:
        result, _ = run_pipeline(
            bands,
            n_rows=payload.n_rows,
            n_cols=payload.n_cols,
            method=payload.method,
            n_zones=payload.n_zones,
            thresholds=thresholds,
        )
    except Exception as exc:
        logger.error(
            "échec du calcul de l'analyse",
            extra={"field_id": field_id, "method": payload.method, "source": payload.source, "reason": str(exc)},
        )
        _raise_api_error(
            422,
            "analysis_error",
            f"Impossible de calculer l'analyse (image probablement inexploitable, ex: entièrement masquée) : {exc}",
        )
    result["field_id"] = field_id
    result["data_quality"] = {
        "valid_pixel_ratio": round(valid_ratio, 3),
        "polygon_applied": bool(field.get("polygon")),
        "low_quality": valid_ratio < LOW_QUALITY_PIXEL_RATIO,
    }
    logger.info(
        "analyse créée",
        extra={
            "field_id": field_id,
            "source": payload.source,
            "method": payload.method,
            "alert_count": result.get("alert_count"),
        },
    )
    return save_analysis(
        get_db_path(),
        field_id,
        source=payload.source,
        method=payload.method,
        parameters=payload.model_dump(mode="json"),
        result=result,
    )


@app.get("/fields/{field_id}/analyses")
def api_list_analyses(
    field_id: int,
    limit: int = Query(20, ge=1, le=100, description="Nombre d'analyses renvoyées (les plus récentes d'abord)"),
    offset: int = Query(0, ge=0),
    user_id: int = Depends(get_current_user_id),
):
    _get_owned_field(field_id, user_id)
    return list_analyses(get_db_path(), field_id, limit=limit, offset=offset)


@app.post("/fields/{field_id}/treatments")
def api_create_treatment(
    field_id: int,
    payload: ZoneTreatmentRequest,
    user_id: int = Depends(get_current_user_id),
):
    _get_owned_field(field_id, user_id)
    try:
        treatment = save_treatment(
            get_db_path(), field_id, payload.analysis_id, payload.zone_id, payload.note,
        )
    except TreatmentError as exc:
        logger.warning(
            "échec d'enregistrement de traitement",
            extra={"field_id": field_id, "analysis_id": payload.analysis_id, "zone_id": payload.zone_id, "reason": str(exc)},
        )
        _raise_api_error(400, "validation_error", str(exc))
    logger.info(
        "traitement enregistré",
        extra={"field_id": field_id, "analysis_id": payload.analysis_id, "zone_id": payload.zone_id},
    )
    return treatment


@app.get("/fields/{field_id}/treatments")
def api_list_treatments(field_id: int, user_id: int = Depends(get_current_user_id)):
    _get_owned_field(field_id, user_id)
    return list_treatments(get_db_path(), field_id)


@app.get("/fields/{field_id}/zones")
def api_field_zones(
    field_id: int,
    method: str = "grid",
    n_rows: int = 8,
    n_cols: int = 12,
    n_zones: int = 6,
    critical_threshold: float | None = None,
    warning_threshold: float | None = None,
    user_id: int = Depends(get_current_user_id),
):
    field = get_field(get_db_path(), field_id)
    if field is None:
        _raise_api_error(404, "not_found", "Champ introuvable.")
    if field["user_id"] != user_id:
        # on renvoie 404 plutôt que 403 pour ne pas révéler l'existence du
        # champ d'un autre utilisateur
        _raise_api_error(404, "not_found", "Champ introuvable.")
    if method not in ("grid", "kmeans"):
        _raise_api_error(422, "validation_error", "method doit être 'grid' ou 'kmeans'.")
    if n_rows < 1 or n_cols < 1:
        _raise_api_error(422, "validation_error", "n_rows et n_cols doivent être des entiers positifs.")
    if n_zones < 1:
        _raise_api_error(422, "validation_error", "n_zones doit être un entier positif.")
    if n_rows > 50 or n_cols > 50:
        _raise_api_error(422, "validation_error", "n_rows et n_cols ne peuvent pas dépasser 50.")
    if n_zones > 20:
        _raise_api_error(422, "validation_error", "n_zones ne peut pas dépasser 20.")

    thresholds = None
    if critical_threshold is not None or warning_threshold is not None:
        thresholds = {
            "critical": critical_threshold,
            "warning": warning_threshold,
        }

    # Mode démo : pas d'accès réseau à Sentinel Hub dans cet environnement.
    # En production, remplacer par : fetch_sentinel.fetch_field_bands(bbox_du_champ)
    bands = generate_synthetic_bands(seed=field_id)
    try:
        result, _ = run_pipeline(
            bands,
            n_rows=n_rows,
            n_cols=n_cols,
            method=method,
            n_zones=n_zones,
            thresholds=thresholds,
        )
    except Exception as exc:
        _raise_api_error(422, "analysis_error", f"Impossible de calculer l'analyse : {exc}")
    result["field_id"] = field_id
    result["field_name"] = field["name"]
    # ces données sont simulées : on le dit explicitement pour que l'interface
    # ne les présente jamais comme une mesure réelle de la parcelle
    result["source"] = "synthetic"
    return result


@app.get("/crops")
def api_crops():
    return CROPS_DISPONIBLES


# Sert l'app mobile (HTML/JS/manifest/icônes) depuis le même serveur que
# l'API : un seul déploiement, même domaine (donc plus besoin de CORS entre
# front et back en production), et l'app devient installable (PWA) puisque
# manifest.json et le service worker doivent être servis en HTTPS réel.
# Monté APRÈS toutes les routes /api pour ne jamais les masquer.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")