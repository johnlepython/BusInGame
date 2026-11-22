"""Chargement des données de jeu (JSON) et création d'une nouvelle partie.

Formats attendus, dans le dossier data/ (voir README) :

bus_models.json : liste d'objets
  name, energy (diesel|gnv|electrique), body (standard|articule), capacity,
  consumption_per_100km, price, reliability (0..1), maintenance_cost_per_km

lines.json : liste d'objets
  code, name, distance_km, duration_min, allowed_body (any|standard), fare,
  puis la demande, au choix :
    - weekday_passengers + weekend_factor [+ profile_weekday / profile_weekend]
      (total de passagers/jour réparti selon un profil horaire nommé)
    - "hourly": {"weekday": {"7": 600, "8": 700, ...}, "weekend": {...}}  (demande explicite par heure)
"""
import json
import sqlite3
from datetime import date
from pathlib import Path

from . import actions, rules

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Profils horaires (poids relatifs, heures 0-23). Normalisés au chargement.
PROFILES = {
    "commuter": {5: 2, 6: 6, 7: 12, 8: 13, 9: 7, 10: 4, 11: 4, 12: 6, 13: 5, 14: 4,
                 15: 5, 16: 8, 17: 10, 18: 7, 19: 4, 20: 2, 21: 1, 22: 1},
    "leisure": {6: 1, 7: 2, 8: 3, 9: 5, 10: 7, 11: 8, 12: 8, 13: 7, 14: 7, 15: 8,
                16: 8, 17: 8, 18: 7, 19: 5, 20: 3, 21: 2, 22: 1},
}
DEFAULT_PROFILE = {"weekday": "commuter", "weekend": "leisure"}

MODEL_FIELDS = ("name", "energy", "body", "capacity", "consumption_per_100km",
                "price", "reliability", "maintenance_cost_per_km")


class DataError(Exception):
    pass


def load_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DataError(f"Fichier de données introuvable : {path}")
    except json.JSONDecodeError as e:
        raise DataError(f"JSON invalide dans {path} : {e}")


def validate_models(models: list[dict]) -> None:
    for m in models:
        missing = [f for f in MODEL_FIELDS if f not in m]
        if missing:
            raise DataError(f"Modèle {m.get('name', '?')} : champs manquants {missing}")
        if m["energy"] not in rules.ENERGY_PRICE:
            raise DataError(f"Modèle {m['name']} : énergie inconnue « {m['energy']} »")
        if m["body"] not in ("standard", "articule"):
            raise DataError(f"Modèle {m['name']} : carrosserie inconnue « {m['body']} »")
        if not 0 < m["reliability"] <= 1:
            raise DataError(f"Modèle {m['name']} : reliability doit être entre 0 et 1")


def hourly_demand(total: float, profile_name: str) -> dict[int, int]:
    if profile_name not in PROFILES:
        raise DataError(f"Profil horaire inconnu : {profile_name} (disponibles : {', '.join(PROFILES)})")
    profile = PROFILES[profile_name]
    weight = sum(profile.values())
    return {h: round(total * w / weight) for h, w in profile.items()}


def line_demand(line: dict) -> dict[str, dict[int, int]]:
    """Retourne {'weekday': {heure: passagers}, 'weekend': {...}} pour une ligne du JSON."""
    if "hourly" in line:
        return {
            dtype: {int(h): int(p) for h, p in line["hourly"][dtype].items()}
            for dtype in ("weekday", "weekend")
        }
    if "weekday_passengers" not in line:
        raise DataError(f"Ligne {line.get('code', '?')} : il faut weekday_passengers ou hourly")
    weekday = line["weekday_passengers"]
    weekend = weekday * line.get("weekend_factor", 0.5)
    return {
        "weekday": hourly_demand(weekday, line.get("profile_weekday", DEFAULT_PROFILE["weekday"])),
        "weekend": hourly_demand(weekend, line.get("profile_weekend", DEFAULT_PROFILE["weekend"])),
    }


def seed_catalog(conn: sqlite3.Connection, data_dir: Path = DATA_DIR) -> None:
    """Charge le catalogue de bus s'il est vide."""
    if conn.execute("SELECT COUNT(*) AS n FROM bus_model").fetchone()["n"]:
        return
    models = load_json(Path(data_dir) / "bus_models.json")
    validate_models(models)
    with conn:
        conn.executemany(
            """INSERT INTO bus_model (name, energy, body, capacity, consumption_per_100km,
                                      price, reliability, maintenance_cost_per_km)
               VALUES (:name, :energy, :body, :capacity, :consumption_per_100km,
                       :price, :reliability, :maintenance_cost_per_km)""",
            [{f: m[f] for f in MODEL_FIELDS} for m in models],
        )


def load_lines(conn: sqlite3.Connection, company_id: int, lines: list[dict]) -> None:
    for line in lines:
        for f in ("code", "name", "distance_km", "duration_min", "fare"):
            if f not in line:
                raise DataError(f"Ligne {line.get('code', '?')} : champ manquant « {f} »")
        allowed = line.get("allowed_body", "any")
        if allowed not in ("any", "standard"):
            raise DataError(f"Ligne {line['code']} : allowed_body doit être any ou standard")
        demand = line_demand(line)
        cur = conn.execute(
            """INSERT INTO line (company_id, code, name, distance_km, duration_min, allowed_body, fare)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (company_id, line["code"], line["name"], line["distance_km"], line["duration_min"],
             allowed, line["fare"]),
        )
        for dtype, hours in demand.items():
            conn.executemany(
                "INSERT INTO line_demand (line_id, day_type, hour, passengers) VALUES (?, ?, ?, ?)",
                [(cur.lastrowid, dtype, h, p) for h, p in hours.items()],
            )


def new_game(conn: sqlite3.Connection, name: str = "Ma Compagnie", seed: int = 1,
             start_cash: float = 250_000.0, data_dir: Path = DATA_DIR,
             start_date: date | None = None, depot_capacity: int = 8) -> int:
    """Crée une compagnie jouable : 1 dépôt, les lignes du JSON, 1 bus + 2 conducteurs sur la 1re ligne."""
    data_dir = Path(data_dir)
    lines = load_json(data_dir / "lines.json")
    seed_catalog(conn, data_dir)
    start = (start_date or date.today()).isoformat()
    with conn:
        cur = conn.execute(
            "INSERT INTO company (name, cash, reputation, seed, start_date) VALUES (?, 0, ?, ?, ?)",
            (name, rules.REPUTATION_START, seed, start),
        )
        company_id = cur.lastrowid
        actions.post(conn, company_id, 0, "capital", start_cash, "Capital initial")
        conn.execute("INSERT INTO depot (company_id, name, capacity) VALUES (?, 'Dépôt principal', ?)",
                     (company_id, depot_capacity))
        load_lines(conn, company_id, lines)

    # Équipement de départ : le modèle standard le moins cher, 2 conducteurs, services plein temps sur la 1re ligne
    depot_id = conn.execute("SELECT id FROM depot WHERE company_id = ?", (company_id,)).fetchone()["id"]
    model = conn.execute(
        "SELECT id FROM bus_model WHERE body = 'standard' ORDER BY price LIMIT 1").fetchone()
    line_id = conn.execute(
        "SELECT id FROM line WHERE company_id = ? ORDER BY id LIMIT 1", (company_id,)).fetchone()["id"]
    vid = actions.buy_vehicle(conn, company_id, model["id"], depot_id)
    for i, (start_h, end_h) in enumerate(((5, 14), (14, 23)), start=1):
        did = actions.hire_driver(conn, company_id, f"Conducteur {i}")
        sid = actions.create_service(conn, line_id, start_h, end_h)
        actions.assign_service(conn, sid, vehicle_id=vid, driver_id=did)
    return company_id
