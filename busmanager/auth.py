"""Comptes joueurs : mots de passe (scrypt) et sessions (jeton aléatoire, seule son empreinte est stockée).

Authentification volontairement simple : pas de réinitialisation de mot de passe, pas de limitation de tentatives.
À durcir (HTTPS obligatoire, limitation de débit) avant toute exposition publique.
"""
import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

SESSION_DAYS = 30
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")
MIN_PASSWORD = 8
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


class AuthError(Exception):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        probe = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
        return hmac.compare_digest(probe, bytes.fromhex(digest))
    except ValueError:
        return False


def create_account(conn: sqlite3.Connection, username: str, password: str) -> int:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise AuthError("Identifiant : 3 à 30 caractères (lettres, chiffres, _ . -)")
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Mot de passe : {MIN_PASSWORD} caractères minimum")
    if conn.execute("SELECT 1 FROM account WHERE username = ?", (username,)).fetchone():
        raise AuthError("Cet identifiant est déjà pris")
    with conn:
        cur = conn.execute("INSERT INTO account (username, password_hash) VALUES (?, ?)",
                           (username, hash_password(password)))
    return cur.lastrowid


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> int:
    row = conn.execute("SELECT id, password_hash FROM account WHERE username = ?",
                       (username.strip(),)).fetchone()
    # Même travail de hachage si le compte n'existe pas, pour ne pas révéler les identifiants valides
    stored = row["password_hash"] if row else hash_password("x" * MIN_PASSWORD)
    if not verify_password(password, stored) or row is None:
        raise AuthError("Identifiant ou mot de passe incorrect")
    return row["id"]


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_session(conn: sqlite3.Connection, account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (_now() + timedelta(days=SESSION_DAYS)).isoformat()
    with conn:
        conn.execute("DELETE FROM session WHERE expires_at < ?", (_now().isoformat(),))
        conn.execute("INSERT INTO session (token_hash, account_id, expires_at) VALUES (?, ?, ?)",
                     (_digest(token), account_id, expires))
    return token


def account_for_token(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    return conn.execute(
        """SELECT a.id, a.username FROM session s JOIN account a ON a.id = s.account_id
           WHERE s.token_hash = ? AND s.expires_at > ?""", (_digest(token), _now().isoformat())).fetchone()


def delete_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        with conn:
            conn.execute("DELETE FROM session WHERE token_hash = ?", (_digest(token),))
