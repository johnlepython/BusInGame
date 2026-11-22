"""Actions du joueur : toutes les écritures « hors tick » passent par ici."""
import sqlite3

from . import rules


class GameError(Exception):
    """Action refusée par les règles du jeu."""


def current_day(conn: sqlite3.Connection, company_id: int) -> int:
    return conn.execute("SELECT game_day FROM company WHERE id = ?", (company_id,)).fetchone()["game_day"]


def post(conn, company_id, day, kind, amount, label="", ref_id=None):
    """Écriture comptable : met à jour le ledger ET la trésorerie (invariant)."""
    conn.execute(
        "INSERT INTO ledger (company_id, day, kind, amount, label, ref_id) VALUES (?, ?, ?, ?, ?, ?)",
        (company_id, day, kind, amount, label, ref_id),
    )
    conn.execute("UPDATE company SET cash = cash + ? WHERE id = ?", (amount, company_id))


def log_event(conn, company_id, day, kind, message, vehicle_id=None):
    conn.execute(
        "INSERT INTO event_log (company_id, day, kind, vehicle_id, message) VALUES (?, ?, ?, ?, ?)",
        (company_id, day, kind, vehicle_id, message),
    )


def _vehicle(conn, vehicle_id):
    v = conn.execute("SELECT * FROM vehicle WHERE id = ?", (vehicle_id,)).fetchone()
    if v is None:
        raise GameError(f"Véhicule {vehicle_id} introuvable")
    return v


def buy_vehicle(conn, company_id: int, model_id: int, depot_id: int) -> int:
    model = conn.execute("SELECT * FROM bus_model WHERE id = ?", (model_id,)).fetchone()
    if model is None:
        raise GameError("Modèle inconnu")
    depot = conn.execute(
        "SELECT * FROM depot WHERE id = ? AND company_id = ?", (depot_id, company_id)
    ).fetchone()
    if depot is None:
        raise GameError("Dépôt inconnu")
    used = conn.execute("SELECT COUNT(*) AS n FROM vehicle WHERE depot_id = ?", (depot_id,)).fetchone()["n"]
    if used >= depot["capacity"]:
        raise GameError("Dépôt plein")
    company = conn.execute("SELECT cash, game_day FROM company WHERE id = ?", (company_id,)).fetchone()
    if company["cash"] < model["price"]:
        raise GameError("Trésorerie insuffisante")
    day = company["game_day"]
    with conn:
        cur = conn.execute(
            """INSERT INTO vehicle (company_id, model_id, depot_id, plate, revision_due_day, ct_due_day)
               VALUES (?, ?, ?, 'TMP', ?, ?)""",
            (company_id, model_id, depot_id,
             day + rules.REVISION_INTERVAL_DAYS, day + rules.CT_INTERVAL_DAYS),
        )
        vid = cur.lastrowid
        conn.execute("UPDATE vehicle SET plate = ? WHERE id = ?", (f"BM-{vid:04d}", vid))
        post(conn, company_id, day, "purchase", -model["price"], f"Achat {model['name']}", vid)
    return vid


def hire_driver(conn, company_id: int, name: str, articulated_licence: bool = False) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO driver (company_id, name, daily_wage, articulated_licence) VALUES (?, ?, ?, ?)",
            (company_id, name, rules.DRIVER_DAILY_WAGE, int(articulated_licence)),
        )
    return cur.lastrowid


def create_service(conn, line_id: int, start_hour: int, end_hour: int, day_type: str = "all") -> int:
    if day_type not in ("all", "weekday", "weekend"):
        raise GameError("Type de jour invalide (all, weekday, weekend)")
    if conn.execute("SELECT 1 FROM line WHERE id = ?", (line_id,)).fetchone() is None:
        raise GameError("Ligne introuvable")
    if not (0 <= start_hour < end_hour <= 24):
        raise GameError("Plage horaire invalide")
    if end_hour - start_hour > rules.MAX_SERVICE_HOURS:
        raise GameError(f"Un service ne peut pas dépasser {rules.MAX_SERVICE_HOURS} h")
    with conn:
        cur = conn.execute(
            "INSERT INTO service (line_id, start_hour, end_hour, day_type) VALUES (?, ?, ?, ?)",
            (line_id, start_hour, end_hour, day_type),
        )
    return cur.lastrowid


def _days_intersect(a: str, b: str) -> bool:
    return a == "all" or b == "all" or a == b


def _clashes(s, others) -> bool:
    """Vrai si le service s chevauche l'un des autres services un même type de jour."""
    return any(
        _days_intersect(s["day_type"], o["day_type"])
        and s["start_hour"] < o["end_hour"] and o["start_hour"] < s["end_hour"]
        for o in others
    )


def _max_daily_hours(s, others) -> int:
    """Heures de conduite max sur un jour de semaine ou de week-end, en comptant s."""
    worst = 0
    for dtype in ("weekday", "weekend"):
        total = sum(o["end_hour"] - o["start_hour"] for o in others if o["day_type"] in ("all", dtype))
        if s["day_type"] in ("all", dtype):
            total += s["end_hour"] - s["start_hour"]
        worst = max(worst, total)
    return worst


def assign_service(conn, service_id: int, vehicle_id: int | None = None, driver_id: int | None = None) -> None:
    """Affecte un bus et/ou un conducteur à un service, en vérifiant les règles."""
    s = conn.execute(
        """SELECT s.*, l.allowed_body, l.company_id FROM service s
           JOIN line l ON l.id = s.line_id WHERE s.id = ?""",
        (service_id,),
    ).fetchone()
    if s is None:
        raise GameError("Service introuvable")

    if vehicle_id is not None:
        v = _vehicle(conn, vehicle_id)
        if v["company_id"] != s["company_id"]:
            raise GameError("Ce bus n'appartient pas à la compagnie")
        body = conn.execute("SELECT body FROM bus_model WHERE id = ?", (v["model_id"],)).fetchone()["body"]
        if s["allowed_body"] == "standard" and body == "articule":
            raise GameError("Bus articulé interdit sur cette ligne")
        others = conn.execute(
            "SELECT start_hour, end_hour, day_type FROM service WHERE vehicle_id = ? AND id != ?",
            (vehicle_id, service_id),
        ).fetchall()
        if _clashes(s, others):
            raise GameError("Ce bus a déjà un service sur cette plage horaire")

    if driver_id is not None:
        d = conn.execute("SELECT * FROM driver WHERE id = ?", (driver_id,)).fetchone()
        if d is None or d["company_id"] != s["company_id"]:
            raise GameError("Conducteur introuvable")
        effective_vehicle = vehicle_id if vehicle_id is not None else s["vehicle_id"]
        if effective_vehicle is not None:
            body = conn.execute(
                "SELECT m.body FROM vehicle v JOIN bus_model m ON m.id = v.model_id WHERE v.id = ?",
                (effective_vehicle,),
            ).fetchone()["body"]
            if body == "articule" and not d["articulated_licence"]:
                raise GameError("Ce conducteur n'a pas le permis articulé")
        others = conn.execute(
            "SELECT start_hour, end_hour, day_type FROM service WHERE driver_id = ? AND id != ?",
            (driver_id, service_id),
        ).fetchall()
        if _clashes(s, others):
            raise GameError("Ce conducteur a déjà un service sur cette plage horaire")
        if _max_daily_hours(s, others) > rules.DRIVER_MAX_HOURS:
            raise GameError(f"Plus de {rules.DRIVER_MAX_HOURS} h de conduite par jour")

    with conn:
        if vehicle_id is not None:
            conn.execute("UPDATE service SET vehicle_id = ? WHERE id = ?", (vehicle_id, service_id))
        if driver_id is not None:
            conn.execute("UPDATE service SET driver_id = ? WHERE id = ?", (driver_id, service_id))


def unassign_service(conn, service_id: int, vehicle: bool = False, driver: bool = False) -> None:
    if conn.execute("SELECT 1 FROM service WHERE id = ?", (service_id,)).fetchone() is None:
        raise GameError("Service introuvable")
    with conn:
        if vehicle:
            conn.execute("UPDATE service SET vehicle_id = NULL WHERE id = ?", (service_id,))
        if driver:
            conn.execute("UPDATE service SET driver_id = NULL WHERE id = ?", (service_id,))


def delete_service(conn, service_id: int) -> None:
    with conn:
        n = conn.execute("DELETE FROM service WHERE id = ?", (service_id,)).rowcount
    if n == 0:
        raise GameError("Service introuvable")


def _workshop_ready(conn, vehicle_id: int, cost: float):
    """Vérifie qu'un bus peut entrer à l'atelier (pas déjà immobilisé) et que la trésorerie suffit."""
    v = _vehicle(conn, vehicle_id)
    if v["status"] == "workshop":
        raise GameError(f"{v['plate']} est déjà à l'atelier")
    day = current_day(conn, v["company_id"])
    cash = conn.execute("SELECT cash FROM company WHERE id = ?", (v["company_id"],)).fetchone()["cash"]
    if cash < cost:
        raise GameError("Trésorerie insuffisante")
    return v, day


def maintain_vehicle(conn, vehicle_id: int) -> None:
    """Révision : coût, +état moteur, repousse l'échéance, 1 jour à l'atelier."""
    v, day = _workshop_ready(conn, vehicle_id, rules.REVISION_COST)
    with conn:
        conn.execute(
            """UPDATE vehicle SET engine_state = MIN(100, engine_state + ?), revision_due_day = ?,
                      status = 'workshop', available_from_day = ? WHERE id = ?""",
            (rules.REVISION_ENGINE_BONUS, day + rules.REVISION_INTERVAL_DAYS, day + 1, vehicle_id),
        )
        post(conn, v["company_id"], day, "revision", -rules.REVISION_COST, f"Révision {v['plate']}", vehicle_id)


def overhaul_engine(conn, vehicle_id: int) -> None:
    """Révision générale : état moteur remis à 100, quelques jours à l'atelier."""
    v, day = _workshop_ready(conn, vehicle_id, rules.OVERHAUL_COST)
    with conn:
        conn.execute(
            """UPDATE vehicle SET engine_state = 100, revision_due_day = ?,
                      status = 'workshop', available_from_day = ? WHERE id = ?""",
            (day + rules.REVISION_INTERVAL_DAYS, day + 1 + rules.OVERHAUL_DOWNTIME_DAYS, vehicle_id),
        )
        post(conn, v["company_id"], day, "overhaul", -rules.OVERHAUL_COST,
             f"Révision générale {v['plate']}", vehicle_id)


def refurbish_interior(conn, vehicle_id: int) -> None:
    """Remise à neuf de l'intérieur : état intérieur remis à 100."""
    v, day = _workshop_ready(conn, vehicle_id, rules.REFURBISH_COST)
    with conn:
        conn.execute(
            "UPDATE vehicle SET interior_state = 100, status = 'workshop', available_from_day = ? WHERE id = ?",
            (day + 1 + rules.REFURBISH_DOWNTIME_DAYS, vehicle_id),
        )
        post(conn, v["company_id"], day, "refurbish", -rules.REFURBISH_COST,
             f"Remise à neuf intérieur {v['plate']}", vehicle_id)


def pass_ct(conn, vehicle_id: int) -> None:
    v = _vehicle(conn, vehicle_id)
    day = current_day(conn, v["company_id"])
    cash = conn.execute("SELECT cash FROM company WHERE id = ?", (v["company_id"],)).fetchone()["cash"]
    if cash < rules.CT_COST:
        raise GameError("Trésorerie insuffisante")
    with conn:
        conn.execute(
            "UPDATE vehicle SET ct_due_day = ? WHERE id = ?", (day + rules.CT_INTERVAL_DAYS, vehicle_id)
        )
        post(conn, v["company_id"], day, "ct", -rules.CT_COST, f"Contrôle technique {v['plate']}", vehicle_id)
