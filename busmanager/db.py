import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str = "game.db", check_same_thread: bool = True) -> sqlite3.Connection:
    """check_same_thread=False pour l'API : FastAPI exécute les fonctions synchrones dans un pool de threads,
    la connexion (ouverte par requête, jamais partagée entre requêtes) peut donc changer de thread."""
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
