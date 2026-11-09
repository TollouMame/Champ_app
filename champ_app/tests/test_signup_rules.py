import numpy as np
import pytest
from fastapi.testclient import TestClient

from src import api
from src.auth import AuthError, COMMON_PASSWORDS, MAX_PASSWORD_LENGTH, validate_password
from src.zoning_kmeans import downsample_label_map


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "app.db")
    with TestClient(api.app) as c:
        yield c


# ---------------------------------------------------------------- règles de mot de passe

@pytest.mark.parametrize("password,fragment", [
    ("abc", "au moins 8"),
    ("1234567", "au moins 8"),
    ("        ", "uniquement d'espaces"),          # 8 espaces : acceptés avant le correctif
    ("aaaaaaaaaa", "caractères différents"),
    ("abababab", "caractères différents"),
    ("password", "trop courant"),
    ("MotDePasse", "trop courant"),                # insensible à la casse
    ("123456789", "trop courant"),
    ("x" * (MAX_PASSWORD_LENGTH + 1), "dépasser"),
])
def test_password_rules_reject_unacceptable_passwords(password, fragment):
    with pytest.raises(AuthError, match=fragment):
        validate_password(password)


@pytest.mark.parametrize("password", [
    "motdepasse123", "Un bon mot de passe !", "  espaces au bord  ", "éèàù12345",
    "ab12" * (MAX_PASSWORD_LENGTH // 4),               # exactement 128 caractères : accepté
])
def test_password_rules_accept_reasonable_passwords(password):
    validate_password(password)


@pytest.mark.parametrize("password", ["abc", "        ", "password", "aaaaaaaaaa"])
def test_api_refuses_signup_and_creates_no_account(client, password):
    response = client.post("/auth/register", json={"email": "refuse@example.fr", "password": password})
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "auth_error"
    # aucun compte « à moitié créé » : le même email peut ensuite s'inscrire normalement
    assert client.post("/auth/login", json={"email": "refuse@example.fr", "password": password}).status_code == 401
    assert client.post("/auth/register", json={"email": "refuse@example.fr", "password": "motdepasse123"}).status_code == 200


def test_too_long_password_gets_a_french_message_not_a_pydantic_one(client):
    """Avant : « String should have at most 128 characters » (anglais, via Pydantic)."""
    response = client.post("/auth/register", json={"email": "long@example.fr", "password": "x" * 129})
    assert response.status_code == 400
    message = response.json()["detail"]["message"]
    assert "dépasser 128 caractères" in message and "String should" not in message


def test_too_long_email_gets_a_french_message(client):
    response = client.post("/auth/register", json={"email": "a" * 300 + "@example.fr", "password": "motdepasse123"})
    assert response.status_code == 400
    assert "trop longue" in response.json()["detail"]["message"]


def test_existing_accounts_with_a_weak_password_can_still_log_in(client, tmp_path):
    """Les nouvelles règles ne s'appliquent qu'à la CRÉATION : on n'enferme personne dehors."""
    import sqlite3
    from src.auth import _hash_password

    stored, salt = _hash_password("password")            # compte créé avant les nouvelles règles
    conn = sqlite3.connect(tmp_path / "app.db")
    conn.execute("INSERT INTO users (email, password_hash, salt) VALUES ('ancien@example.fr', ?, ?)", (stored, salt))
    conn.commit(); conn.close()
    assert client.post("/auth/login", json={"email": "ancien@example.fr", "password": "password"}).status_code == 200


# ---------------------------------------------------------------- proportions de la carte

@pytest.mark.parametrize("h,w,expected", [
    (120, 160, (30, 40)),     # 4:3 : inchangé
    (50, 200, (10, 40)),      # champ très allongé : 4:1
    (200, 50, (30, 8)),       # champ très haut : 1:4 (arrondi de 30 x 50/200 = 7,5)
    (100, 100, (30, 30)),     # carré
    (5, 5, (30, 30)),         # minuscule : agrandi sans déformation
    (3, 300, (1, 40)),        # extrême : au moins une ligne
])
def test_label_grid_keeps_the_proportions_of_the_field(h, w, expected):
    grid = downsample_label_map(np.zeros((h, w), dtype=int))
    assert (len(grid), len(grid[0])) == expected


def test_label_grid_samples_the_cell_centres():
    raster = np.zeros((4, 8), dtype=int)
    raster[:, 4:] = 1                                  # moitié droite = zone 1
    grid = np.array(downsample_label_map(raster, out_rows=30, out_cols=40))
    assert grid.shape == (20, 40)
    assert (grid[:, :20] == 0).all() and (grid[:, 20:] == 1).all()


def test_analysis_label_grid_matches_the_declared_dimensions(client):
    token = client.post("/auth/register", json={"email": "carte@example.fr", "password": "motdepasse123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    field_id = client.post("/fields", headers=headers, json={"name": "P", "crop": "Orge", "bbox": [1.5, 48.3, 1.505, 48.304]}).json()["id"]
    grid = client.post(f"/fields/{field_id}/analyses", headers=headers, json={"method": "kmeans"}).json()["result"]["label_grid"]
    assert grid["rows"] == len(grid["data"]) and grid["cols"] == len(grid["data"][0])
