"""Simulation automatique : sert à vérifier l'équilibrage de l'économie sur de longues durées.

La stratégie est volontairement simple (gloutonne) : elle fait les entretiens à temps, équipe les bus de valideurs,
remplit la citerne de diesel et, quand la trésorerie le permet, achète un bus pour la ligne dont la fréquence
cible est la moins bien tenue.
"""
import math
from datetime import date

from . import actions, balance, engine, rules, seed
from .db import connect, init_db

CASH_RESERVE = 40_000.0  # trésorerie à garder après un achat


def _line_deficit(conn, line) -> tuple[float, bool]:
    """Bus manquants (somme sur les heures d'un jour de semaine) et si le déficit touche les heures creuses."""
    freq = {r["band"]: r["departures_per_hour"] for r in conn.execute(
        "SELECT band, departures_per_hour FROM line_frequency WHERE line_id = ? AND day_type = 'weekday'",
        (line["id"],))}
    services = conn.execute(
        """SELECT start_hour, end_hour FROM service WHERE line_id = ? AND vehicle_id IS NOT NULL
           AND driver_id IS NOT NULL AND day_type IN ('all', 'weekday')""", (line["id"],)).fetchall()
    deficit, off_peak = 0.0, False
    for hour in range(rules.FIRST_HOUR, rules.LAST_HOUR + 1):
        band = rules.band_of("weekday", hour)
        need = math.ceil(freq[band] / rules.departures_per_bus_hour(line["duration_min"]) - 1e-9)
        have = sum(1 for s in services if s["start_hour"] <= hour < s["end_hour"])
        if need > have:
            deficit += need - have
            off_peak = off_peak or band == "creuse"
    return deficit, off_peak


def _add_bus_for_line(conn, company_id, line, full_day: bool):
    model = balance.reference_model(conn, line)
    depot = conn.execute("SELECT * FROM depot WHERE company_id = ?", (company_id,)).fetchone()
    vid = actions.buy_vehicle(conn, company_id, model["id"], depot["id"])
    windows = ((5, 14), (14, 23)) if full_day else ((7, 9), (16, 19))
    n = conn.execute("SELECT COUNT(*) AS n FROM driver WHERE company_id = ?", (company_id,)).fetchone()["n"]
    count = 2 if full_day else 1
    dids = [actions.hire_driver(conn, company_id, f"Conducteur {n + i + 1}") for i in range(count)]
    for i, (start, end) in enumerate(windows):
        sid = actions.create_service(conn, line["id"], start, end)
        actions.assign_service(conn, sid, vehicle_id=vid, driver_id=dids[min(i, count - 1)])


def greedy_strategy(conn, company_id):
    day = actions.current_day(conn, company_id)

    # Entretien préventif et options
    for v in conn.execute("SELECT * FROM vehicle WHERE company_id = ?", (company_id,)).fetchall():
        try:
            if v["ct_due_day"] - day <= 7:
                actions.pass_ct(conn, v["id"])
            if v["status"] != "active":
                continue
            if v["engine_state"] < 35:
                actions.overhaul_engine(conn, v["id"])
            elif v["interior_state"] < 25:
                actions.refurbish_interior(conn, v["id"])
            elif v["revision_due_day"] - day <= 3:
                actions.maintain_vehicle(conn, v["id"])
            elif not conn.execute(
                    """SELECT 1 FROM vehicle_option vo JOIN option o ON o.id = vo.option_id
                       WHERE vo.vehicle_id = ? AND o.code = 'valideur'""", (v["id"],)).fetchone():
                if actions.option_stock(conn, company_id, "valideur") < 1:
                    actions.buy_option(conn, company_id, "valideur", 1)
                actions.install_option(conn, v["id"], "valideur")
        except actions.GameError:
            pass

    # Carburant : on remplit la citerne quand elle passe sous 40 %
    for tank in conn.execute(
            """SELECT i.* FROM energy_infra i JOIN depot d ON d.id = i.depot_id
               WHERE d.company_id = ? AND i.energy IN ('diesel', 'gnv')""", (company_id,)).fetchall():
        if tank["stock"] < 0.4 * tank["capacity"]:
            try:
                actions.buy_fuel(conn, company_id, tank["depot_id"], tank["energy"],
                                 tank["capacity"] - tank["stock"])
            except actions.GameError:
                pass

    # Investissement : un bus pour la ligne dont la fréquence est la moins bien tenue
    best = None
    for line in conn.execute("SELECT * FROM line WHERE company_id = ? ORDER BY id", (company_id,)).fetchall():
        deficit, off_peak = _line_deficit(conn, line)
        if deficit > 0 and (best is None or deficit > best[0]):
            best = (deficit, line, off_peak)
    if best:
        cash = conn.execute("SELECT cash FROM company WHERE id = ?", (company_id,)).fetchone()["cash"]
        model = balance.reference_model(conn, best[1])
        if cash > model["price"] + CASH_RESERVE:
            try:
                _add_bus_for_line(conn, company_id, best[1], full_day=best[2])
            except actions.GameError:
                pass  # dépôt plein, etc.


def simulate(days: int = 200, seed_value: int = 1, strategy=greedy_strategy, verbose: bool = True) -> dict:
    conn = connect(":memory:")
    init_db(conn)
    company_id = seed.new_game(conn, seed=seed_value, start_date=date(2026, 1, 5))  # un lundi
    for _ in range(days):
        if strategy:
            strategy(conn, company_id)
        r = engine.run_day(conn, company_id)
        if verbose and (r["day"] + 1) % 30 == 0:
            fleet = conn.execute("SELECT COUNT(*) AS n FROM vehicle").fetchone()["n"]
            rep = conn.execute("SELECT reputation FROM company").fetchone()["reputation"]
            print(f"Jour {r['day'] + 1:>4} | trésorerie {r['cash_after']:>11,.0f} € | "
                  f"résultat du jour {r['profit']:>7,.0f} € | parc {fleet:>2} | réputation {rep:5.1f} | "
                  f"transportés {100 * r['served'] / max(r['demand'], 1):3.0f} % | "
                  f"fréquence {100 * r['delivered_departures'] / max(r['target_departures'], 1):3.0f} %")
    final = conn.execute("SELECT * FROM company").fetchone()
    return {"cash": final["cash"], "day": final["game_day"], "reputation": final["reputation"], "conn": conn}
