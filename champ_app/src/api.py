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

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from src import tokens as tokens_module
from src.db import init_db
from src.auth import register_user, authenticate_user, AuthError
from src.analyses import save_analysis, list_analyses
from src.treatments import save_treatment, list_treatments, TreatmentError
from src.fields import register_field, list_fields, get_field, FieldError, CROPS_DISPONIBLES
from src.config import load_environment
from src.demo_data import generate_synthetic_bands
from src.fetch_sentinel import SentinelHubConfigError, fetch_field_bands
from src.geometry import LOW_QUALITY_PIXEL_RATIO, MIN_VALID_PIXEL_RATIO, prepare_bands
from src.logging_config import setup_logging, get_logger
from src.main import run_pipeline
from src.zoning import get_thresholds
from src.tokens import is_production_environment

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


app = FastAPI(title="Suivi de champs — API", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _handle_rate_limit_exceeded)


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
        return tokens_module.decode_access_token(token)
    except tokens_module.TokenError as e:
        _raise_api_error(401, "authentication_error", str(e))

# Config CORS configurable via variable d'environnement pour éviter l'ouverture totale en production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def api_health():
    return {
        "status": "ok",
        "database": "ready",
        "environment": "production" if is_production_environment() else "development",
        "database_path": str(get_db_path()),
    }


# ---------- schémas de requête/réponse ----------

class RegisterRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


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
            extra={"request_id": getattr(request.state, "request_id", None), "email": email, "reason": str(e)},
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
                "email": payload.email.strip().lower(),
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
def api_create_analysis(
    field_id: int,
    payload: AnalysisRequest,
    user_id: int = Depends(get_current_user_id),
):
    field = _get_owned_field(field_id, user_id)
    if payload.source not in ("synthetic", "sentinel"):
        _raise_api_error(422, "validation_error", "source doit être 'synthetic' ou 'sentinel'.")
    if payload.method not in ("grid", "kmeans"):
        _raise_api_error(422, "validation_error", "method doit être 'grid' ou 'kmeans'.")
    if payload.n_rows < 1 or payload.n_cols < 1 or payload.n_zones < 1 or payload.resolution < 1:
        _raise_api_error(422, "validation_error", "Les dimensions de zonage doivent être des entiers positifs.")
    if payload.n_rows > 50 or payload.n_cols > 50:
        _raise_api_error(422, "validation_error", "n_rows et n_cols ne peuvent pas dépasser 50.")
    if payload.n_zones > 20:
        _raise_api_error(422, "validation_error", "n_zones ne peut pas dépasser 20.")
    if payload.resolution > 100:
        _raise_api_error(422, "validation_error", "resolution ne peut pas dépasser 100.")

    thresholds = {
        "critical": payload.critical_threshold,
        "warning": payload.warning_threshold,
    }
    try:
        get_thresholds(thresholds)
    except ValueError as exc:
        _raise_api_error(422, "validation_error", str(exc))
    if payload.source == "synthetic":
        bands = generate_synthetic_bands(seed=field_id)
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
            _raise_api_error(502, "sentinel_request_error", f"Impossible de récupérer les données Sentinel : {exc}")
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
def api_list_analyses(field_id: int, user_id: int = Depends(get_current_user_id)):
    _get_owned_field(field_id, user_id)
    return list_analyses(get_db_path(), field_id)


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