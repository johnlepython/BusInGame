"""Simulation automatique : sert à vérifier l'équilibrage de l'économie sur de longues durées.

La stratégie est volontairement simple (gloutonne) : elle fait les entretiens à temps et, quand la
trésorerie le permet, achète un bus pour la ligne qui laisse le plus de passagers sans transport.
"""
import sqlite3

from . import actions, engine, rules, seed
from .db import connect, init_db

CASH_RESERVE = 60_000.0  # trésorerie à garder avant d'investir


def _worst_served_line(conn, company_id, day):
    """Ligne avec le plus de passagers non transportés lors du dernier jour calculé."""
    row = conn.execute(
        """SELECT line_id, demand - served AS missed FROM daily_report
           WHERE company_id = ? AND day = ? ORDER BY missed DESC LIMIT 1""",
        (company_id, day - 1),
    ).fetchone()
    return row["line_id"] if row and row["missed"] > 50 else None


def _add_bus_for_line(conn, company_id, line_id):
    line = conn.execute("SELECT * FROM line WHERE id = ?", (line_id,)).fetchone()
    model = conn.execute(
        "SELECT * FROM bus_model WHERE energy = 'diesel' AND body = 'standard'"
    ).fetchone()
    depot = conn.execute("SELECT * FROM depot WHERE company_id = ?", (company_id,)).fetchone()
    vid = actions.buy_vehicle(conn, company_id, model["id"], depot["id"])
    has_full_day = conn.execute(
        "SELECT COUNT(*) AS n FROM service WHERE line_id = ? AND vehicle_id IS NOT NULL", (line_id,)
    ).fetchone()["n"] > 0
    windows = ((6, 10), (16, 20)) if has_full_day else ((5, 14), (14, 23))
    drivers_needed = 1 if has_full_day else 2
    n = conn.execute("SELECT COUNT(*) AS n FROM driver WHERE company_id = ?", (company_id,)).fetchone()["n"]
    dids = [actions.hire_driver(conn, company_id, f"Conducteur {n + i + 1}") for i in range(drivers_needed)]
    for i, (start, end) in enumerate(windows):
        sid = actions.create_service(conn, line["id"], start, end)
        actions.assign_service(conn, sid, vehicle_id=vid, driver_id=dids[min(i, len(dids) - 1)])


def greedy_strategy(conn, company_id):
    day = actions.current_day(conn, company_id)
    cash = conn.execute("SELECT cash FROM company WHERE id = ?", (company_id,)).fetchone()["cash"]

    # Entretien préventif
    for v in conn.execute("SELECT * FROM vehicle WHERE company_id = ?", (company_id,)).fetchall():
        try:
            if v["ct_due_day"] - day <= 7:
                actions.pass_ct(conn, v["id"])
            elif v["revision_due_day"] - day <= 3 and v["status"] == "active":
                actions.maintain_vehicle(conn, v["id"])
        except actions.GameError:
            pass

    # Investissement
    if cash > CASH_RESERVE + 150_000 and day > 0:
        line_id = _worst_served_line(conn, company_id, day)
        if line_id is not None:
            try:
                _add_bus_for_line(conn, company_id, line_id)
            except actions.GameError:
                pass  # dépôt plein, etc.


def simulate(days: int = 200, seed_value: int = 1, strategy=greedy_strategy, verbose: bool = True) -> dict:
    conn = connect(":memory:")
    init_db(conn)
    company_id = seed.new_game(conn, seed=seed_value)
    for _ in range(days):
        if strategy:
            strategy(conn, company_id)
        r = engine.run_day(conn, company_id)
        if verbose and (r["day"] + 1) % 30 == 0:
            fleet = conn.execute("SELECT COUNT(*) AS n FROM vehicle").fetchone()["n"]
            rep = conn.execute("SELECT reputation FROM company").fetchone()["reputation"]
            print(f"Jour {r['day'] + 1:>4} | trésorerie {r['cash_after']:>12,.0f} € | "
                  f"bénéfice du jour {r['profit']:>8,.0f} € | parc {fleet:>2} | réputation {rep:5.1f} | "
                  f"service {100 * r['served'] / max(r['demand'], 1):4.0f} %")
    final = conn.execute("SELECT * FROM company").fetchone()
    return {"cash": final["cash"], "day": final["game_day"], "reputation": final["reputation"], "conn": conn}
