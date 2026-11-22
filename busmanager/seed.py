"""Données de départ : catalogue de bus, nouvelle partie (dépôt, lignes, demande, services)."""
import sqlite3

from . import actions, rules

# (nom, énergie, carrosserie, capacité, conso/100 km, prix, fiabilité, entretien €/km)
BUS_MODELS = [
    ("Standard 12 m diesel",    "diesel",     "standard", 90,  38.0,  95_000, 0.985, 0.18),
    ("Standard 12 m GNV",       "gnv",        "standard", 90,  36.0, 120_000, 0.983, 0.17),
    ("Standard 12 m électrique", "electrique", "standard", 85, 110.0, 210_000, 0.990, 0.10),
    ("Articulé 18 m diesel",    "diesel",     "articule", 150, 50.0, 170_000, 0.980, 0.26),
    ("Articulé 18 m électrique", "electrique", "articule", 140, 150.0, 340_000, 0.988, 0.15),
]

# Poids horaires de la demande (heures 5 à 22), normalisés ensuite
WEEKDAY_PROFILE = {5: 2, 6: 6, 7: 12, 8: 13, 9: 7, 10: 4, 11: 4, 12: 6, 13: 5, 14: 4,
                   15: 5, 16: 8, 17: 10, 18: 7, 19: 4, 20: 2, 21: 1, 22: 1}
WEEKEND_PROFILE = {6: 1, 7: 2, 8: 3, 9: 5, 10: 7, 11: 8, 12: 8, 13: 7, 14: 7, 15: 8,
                   16: 8, 17: 8, 18: 7, 19: 5, 20: 3, 21: 2, 22: 1}

# (code, nom, km, durée min, carrosserie autorisée, tarif, passagers/jour en semaine, facteur week-end)
LINES = [
    ("L1", "Centre-ville ↔ Université",   6.0, 25, "any",      1.50, 2600, 0.55),
    ("L2", "Centre-ville ↔ Zone nord",   14.0, 35, "standard", 2.20, 1100, 0.40),
    ("L3", "Gare ↔ Zone industrielle",    9.0, 20, "any",      2.00, 800, 0.30),
]


def seed_catalog(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) AS n FROM bus_model").fetchone()["n"]:
        return
    with conn:
        conn.executemany(
            """INSERT INTO bus_model (name, energy, body, capacity, consumption_per_100km,
                                      price, reliability, maintenance_cost_per_km)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            BUS_MODELS,
        )


def _hourly(total: float, profile: dict[int, int]) -> dict[int, int]:
    weight = sum(profile.values())
    return {h: round(total * w / weight) for h, w in profile.items()}


def new_game(conn: sqlite3.Connection, name: str = "Ma Compagnie", seed: int = 1,
             start_cash: float = 400_000.0) -> int:
    """Crée une compagnie jouable : 1 dépôt, 3 lignes, 1 bus + 2 conducteurs sur L1."""
    seed_catalog(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO company (name, cash, reputation, seed) VALUES (?, 0, ?, ?)",
            (name, rules.REPUTATION_START, seed),
        )
        company_id = cur.lastrowid
        actions.post(conn, company_id, 0, "capital", start_cash, "Capital initial")

        conn.execute("INSERT INTO depot (company_id, name, capacity) VALUES (?, 'Dépôt principal', 10)",
                     (company_id,))
        for code, lname, km, minutes, body, fare, weekday, we_factor in LINES:
            lc = conn.execute(
                """INSERT INTO line (company_id, code, name, distance_km, duration_min, allowed_body, fare)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (company_id, code, lname, km, minutes, body, fare),
            )
            line_id = lc.lastrowid
            for dtype, total, profile in (
                ("weekday", weekday, WEEKDAY_PROFILE),
                ("weekend", weekday * we_factor, WEEKEND_PROFILE),
            ):
                conn.executemany(
                    "INSERT INTO line_demand (line_id, day_type, hour, passengers) VALUES (?, ?, ?, ?)",
                    [(line_id, dtype, h, p) for h, p in _hourly(total, profile).items()],
                )

    # Équipement de départ : 1 bus diesel, 2 conducteurs, services plein temps sur L1
    depot_id = conn.execute("SELECT id FROM depot WHERE company_id = ?", (company_id,)).fetchone()["id"]
    model_id = conn.execute("SELECT id FROM bus_model WHERE name = 'Standard 12 m diesel'").fetchone()["id"]
    line_id = conn.execute("SELECT id FROM line WHERE company_id = ? AND code = 'L1'", (company_id,)).fetchone()["id"]
    vid = actions.buy_vehicle(conn, company_id, model_id, depot_id)
    for i, (start, end) in enumerate(((5, 14), (14, 23)), start=1):
        did = actions.hire_driver(conn, company_id, f"Conducteur {i}")
        sid = actions.create_service(conn, line_id, start, end)
        actions.assign_service(conn, sid, vehicle_id=vid, driver_id=did)
    return company_id
