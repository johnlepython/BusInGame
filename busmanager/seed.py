"""Chargement des données de jeu (JSON) et création d'une compagnie.

Fichiers du dossier data/ (voir README pour le détail des formats) :
  bus_models.json  catalogue : modèles avec leurs motorisations
  options.json     options installables (valideur…)
  lines.json       lignes du niveau 1 : trajet, fréquences cibles, demande, tarif
  game.json        départ : capital, dépôt, citernes, stock d'options, flotte initiale
"""
import json
import sqlite3
from datetime import date
from pathlib import Path

from . import actions, rules

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

BODIES = ("standard", "articule", "midi")
STATUSES = ("neuf", "occasion")

# Valeurs par défaut quand le JSON ne précise pas "reliability" / "maintenance_cost_per_km" pour une motorisation
BASE_RELIABILITY = {"diesel": 0.985, "gnv": 0.983, "hybride": 0.984, "electrique": 0.989}
USED_RELIABILITY_MALUS = 0.010
MAINTENANCE_PER_KM = {"midi": 0.25, "standard": 0.30, "articule": 0.42}
MAINTENANCE_ENERGY_FACTOR = {"diesel": 1.0, "gnv": 1.05, "hybride": 1.10, "electrique": 0.65}
USED_MAINTENANCE_FACTOR = 1.25


class DataError(Exception):
    pass


def load_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DataError(f"Fichier de données introuvable : {path}")
    except json.JSONDecodeError as e:
        raise DataError(f"JSON invalide dans {path} : {e}")


# ---------- Catalogue ----------

def expand_models(raw: list[dict]) -> list[dict]:
    """Transforme les modèles du JSON (avec leurs motorisations) en lignes de la table bus_model."""
    rows = []
    for m in raw:
        label = f"{m.get('brand', '?')} {m.get('model', '?')}"
        for f in ("brand", "model", "body", "status", "capacity", "motors"):
            if f not in m:
                raise DataError(f"Modèle {label} : champ manquant « {f} »")
        if m["body"] not in BODIES:
            raise DataError(f"Modèle {label} : carrosserie inconnue « {m['body']} » ({', '.join(BODIES)})")
        if m["status"] not in STATUSES:
            raise DataError(f"Modèle {label} : statut inconnu « {m['status']} » ({', '.join(STATUSES)})")
        if m["capacity"] <= 0:
            raise DataError(f"Modèle {label} : capacité invalide")
        initial = m.get("initial", {})
        for motor in m["motors"]:
            energy = motor.get("energy")
            if energy not in rules.ENERGY_PRICE:
                raise DataError(f"Modèle {label} : énergie inconnue « {energy} »")
            for f in ("consumption_per_100km", "price"):
                if f not in motor:
                    raise DataError(f"Modèle {label} ({energy}) : champ manquant « {f} »")
            if energy == "electrique" and "range_km" not in motor:
                raise DataError(f"Modèle {label} : une motorisation électrique exige range_km (autonomie)")
            used = m["status"] == "occasion"
            reliability = motor.get(
                "reliability", BASE_RELIABILITY[energy] - (USED_RELIABILITY_MALUS if used else 0))
            if not 0 < reliability <= 1:
                raise DataError(f"Modèle {label} : reliability doit être entre 0 et 1")
            maintenance = motor.get(
                "maintenance_cost_per_km",
                round(MAINTENANCE_PER_KM[m["body"]] * MAINTENANCE_ENERGY_FACTOR[energy]
                      * (USED_MAINTENANCE_FACTOR if used else 1.0), 3))
            suffix = f"{motor['range_km']:g} km" if energy == "electrique" else energy
            rows.append({
                "brand": m["brand"], "model": m["model"], "name": f"{label} — {suffix}",
                "status": m["status"], "body": m["body"], "energy": energy, "capacity": m["capacity"],
                "consumption_per_100km": motor["consumption_per_100km"], "range_km": motor.get("range_km"),
                "price": motor["price"], "reliability": reliability, "maintenance_cost_per_km": maintenance,
                "initial_km": initial.get("km", 0), "initial_engine_state": initial.get("engine_state", 100),
                "initial_interior_state": initial.get("interior_state", 100),
                "initial_ct_days": initial.get("ct_days", rules.CT_INTERVAL_DAYS),
                "initial_revision_days": initial.get("revision_days", rules.REVISION_INTERVAL_DAYS),
            })
    names = [r["name"] for r in rows]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise DataError(f"Motorisations en double dans le catalogue : {', '.join(sorted(dup))}")
    return rows


def seed_catalog(conn: sqlite3.Connection, data_dir: Path = DATA_DIR) -> None:
    """Charge le catalogue de bus et les options s'ils sont vides."""
    data_dir = Path(data_dir)
    if not conn.execute("SELECT COUNT(*) AS n FROM bus_model").fetchone()["n"]:
        rows = expand_models(load_json(data_dir / "bus_models.json"))
        with conn:
            conn.executemany(
                """INSERT INTO bus_model (brand, model, name, status, body, energy, capacity,
                       consumption_per_100km, range_km, price, reliability, maintenance_cost_per_km,
                       initial_km, initial_engine_state, initial_interior_state, initial_ct_days,
                       initial_revision_days)
                   VALUES (:brand, :model, :name, :status, :body, :energy, :capacity,
                       :consumption_per_100km, :range_km, :price, :reliability, :maintenance_cost_per_km,
                       :initial_km, :initial_engine_state, :initial_interior_state, :initial_ct_days,
                       :initial_revision_days)""",
                rows)
    if not conn.execute("SELECT COUNT(*) AS n FROM option").fetchone()["n"]:
        options = load_json(data_dir / "options.json")
        for o in options:
            for f in ("code", "name", "price"):
                if f not in o:
                    raise DataError(f"Option {o.get('code', '?')} : champ manquant « {f} »")
        with conn:
            conn.executemany(
                "INSERT INTO option (code, name, price, install_days, description) VALUES (?, ?, ?, ?, ?)",
                [(o["code"], o["name"], o["price"], o.get("install_days", 1), o.get("description", ""))
                 for o in options])


# ---------- Lignes ----------

def _check_per_day_type(line: dict, key: str, inner_keys: tuple) -> None:
    code = line["code"]
    if key not in line:
        raise DataError(f"Ligne {code} : champ manquant « {key} »")
    for dtype in rules.DAY_TYPES:
        if dtype not in line[key]:
            raise DataError(f"Ligne {code} : « {key} » doit couvrir {', '.join(rules.DAY_TYPES)} (manque {dtype})")
        for k in inner_keys:
            if k not in line[key][dtype]:
                raise DataError(f"Ligne {code} : « {key} » → {dtype} → {k} manquant")


def hourly_demand(line: dict) -> dict[str, dict[int, int]]:
    """Demande par heure, à partir de demand_per_hour (par tranche) ou de hourly (explicite)."""
    if "hourly" in line:
        return {dtype: {int(h): int(p) for h, p in line["hourly"].get(dtype, {}).items()}
                for dtype in rules.DAY_TYPES}
    _check_per_day_type(line, "demand_per_hour", rules.BANDS)
    return {
        dtype: {h: line["demand_per_hour"][dtype][band]
                for band in rules.BANDS for h in rules.band_hours(dtype, band)}
        for dtype in rules.DAY_TYPES
    }


def load_lines(conn: sqlite3.Connection, company_id: int, lines: list[dict]) -> None:
    for line in lines:
        for f in ("code", "name", "distance_km", "duration_min", "fare", "allowed_bodies"):
            if f not in line:
                raise DataError(f"Ligne {line.get('code', '?')} : champ manquant « {f} »")
        code = str(line["code"])
        line["code"] = code
        bodies = line["allowed_bodies"]
        if not bodies or any(b not in BODIES for b in bodies):
            raise DataError(f"Ligne {code} : allowed_bodies doit contenir parmi {', '.join(BODIES)}")
        if line["duration_min"] <= 0 or line["distance_km"] <= 0:
            raise DataError(f"Ligne {code} : distance et durée doivent être positives")
        _check_per_day_type(line, "frequency", rules.BANDS)
        demand = hourly_demand(line)
        terminus = line.get("terminus", ["", ""])
        cur = conn.execute(
            """INSERT INTO line (company_id, code, name, terminus_a, terminus_b, distance_km, duration_min,
                                 allowed_bodies, fare)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (company_id, code, line["name"], terminus[0], terminus[1], line["distance_km"],
             line["duration_min"], ",".join(bodies), line["fare"]))
        line_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO line_frequency (line_id, day_type, band, departures_per_hour) VALUES (?, ?, ?, ?)",
            [(line_id, d, b, line["frequency"][d][b]) for d in rules.DAY_TYPES for b in rules.BANDS])
        for dtype, hours in demand.items():
            conn.executemany(
                "INSERT INTO line_demand (line_id, day_type, hour, passengers) VALUES (?, ?, ?, ?)",
                [(line_id, dtype, h, p) for h, p in hours.items()])


# ---------- Nouvelle compagnie ----------

def new_game(conn: sqlite3.Connection, name: str = "Ma Compagnie", seed: int = 1,
             data_dir: Path = DATA_DIR, start_date: date | None = None,
             account_id: int | None = None) -> int:
    """Crée une compagnie de niveau 1 : dépôt, citernes, lignes, stock d'options et flotte de départ."""
    data_dir = Path(data_dir)
    settings = load_json(data_dir / "game.json")
    lines = load_json(data_dir / "lines.json")
    seed_catalog(conn, data_dir)

    fleet = settings.get("start_fleet", [])
    models = {}
    for entry in fleet:
        m = conn.execute("SELECT * FROM bus_model WHERE name = ?", (entry["model"],)).fetchone()
        if m is None:
            raise DataError(f"game.json : modèle inconnu « {entry['model']} »")
        models[entry["model"]] = m
    # start_cash est la trésorerie une fois la flotte de départ achetée : le capital l'inclut.
    capital = settings.get("start_cash", 300_000) + sum(models[e["model"]]["price"] for e in fleet)

    start = (start_date or date.today()).isoformat()
    with conn:
        cur = conn.execute(
            "INSERT INTO company (account_id, name, cash, reputation, seed, start_date) VALUES (?, ?, 0, ?, ?, ?)",
            (account_id, name, rules.REPUTATION_START, seed, start))
        company_id = cur.lastrowid
        actions.post(conn, company_id, 0, "capital", capital, "Capital initial")
        depot_id = conn.execute(
            "INSERT INTO depot (company_id, name, capacity) VALUES (?, 'Dépôt principal', ?)",
            (company_id, settings.get("depot_capacity", 24))).lastrowid
        for energy, t in settings.get("start_tanks", {}).items():
            if energy not in rules.TANK_COST_PER_UNIT and energy != "electrique":
                raise DataError(f"game.json : énergie inconnue « {energy} » dans start_tanks")
            conn.execute(
                "INSERT INTO energy_infra (depot_id, energy, capacity, stock) VALUES (?, ?, ?, ?)",
                (depot_id, energy, t["capacity"], min(t.get("stock", 0), t["capacity"])))
        load_lines(conn, company_id, lines)
        for code, qty in settings.get("start_option_stock", {}).items():
            opt = conn.execute("SELECT id FROM option WHERE code = ?", (code,)).fetchone()
            if opt is None:
                raise DataError(f"game.json : option inconnue « {code} »")
            conn.execute("INSERT INTO option_stock (company_id, option_id, quantity) VALUES (?, ?, ?)",
                         (company_id, opt["id"], qty))

    # Flotte de départ : un bus et deux conducteurs en service toute la journée sur la ligne indiquée
    for i, entry in enumerate(fleet, start=1):
        line = conn.execute("SELECT * FROM line WHERE company_id = ? AND code = ?",
                            (company_id, str(entry["line"]))).fetchone()
        if line is None:
            raise DataError(f"game.json : ligne inconnue « {entry['line']} »")
        vid = actions.buy_vehicle(conn, company_id, models[entry["model"]]["id"], depot_id)
        for code in entry.get("options", []):
            opt = conn.execute("SELECT id FROM option WHERE code = ?", (code,)).fetchone()
            if opt is None:
                raise DataError(f"game.json : option inconnue « {code} »")
            with conn:
                conn.execute("INSERT INTO vehicle_option (vehicle_id, option_id, installed_day) VALUES (?, ?, 0)",
                             (vid, opt["id"]))
        for j, (start_h, end_h) in enumerate(((5, 14), (14, 23)), start=1):
            did = actions.hire_driver(conn, company_id, f"Conducteur {2 * i - 2 + j}")
            sid = actions.create_service(conn, line["id"], start_h, end_h)
            actions.assign_service(conn, sid, vehicle_id=vid, driver_id=did)
    return company_id
