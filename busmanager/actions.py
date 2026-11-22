"""Actions du joueur : toutes les écritures « hors tick » passent par ici."""
import sqlite3

from . import rules


class GameError(Exception):
    """Action refusée par les règles du jeu."""


class NotFound(GameError):
    """Objet inexistant, ou qui n'appartient pas à la compagnie (même réponse : on ne révèle rien)."""


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


# ---------- Propriété des objets ----------

_OWNERSHIP_SQL = {
    "vehicle": ("Véhicule", "SELECT 1 FROM vehicle WHERE id = ? AND company_id = ?"),
    "driver": ("Conducteur", "SELECT 1 FROM driver WHERE id = ? AND company_id = ?"),
    "depot": ("Dépôt", "SELECT 1 FROM depot WHERE id = ? AND company_id = ?"),
    "line": ("Ligne", "SELECT 1 FROM line WHERE id = ? AND company_id = ?"),
    "service": ("Service", "SELECT 1 FROM service s JOIN line l ON l.id = s.line_id "
                           "WHERE s.id = ? AND l.company_id = ?"),
}


def ensure_owns(conn, company_id: int, kind: str, obj_id: int) -> None:
    label, sql = _OWNERSHIP_SQL[kind]
    if conn.execute(sql, (obj_id, company_id)).fetchone() is None:
        raise NotFound(f"{label} {obj_id} introuvable")


def _vehicle(conn, vehicle_id):
    v = conn.execute("SELECT * FROM vehicle WHERE id = ?", (vehicle_id,)).fetchone()
    if v is None:
        raise NotFound(f"Véhicule {vehicle_id} introuvable")
    return v


def _cash(conn, company_id) -> float:
    return conn.execute("SELECT cash FROM company WHERE id = ?", (company_id,)).fetchone()["cash"]


# ---------- Achats et personnel ----------

def buy_vehicle(conn, company_id: int, model_id: int, depot_id: int) -> int:
    model = conn.execute("SELECT * FROM bus_model WHERE id = ?", (model_id,)).fetchone()
    if model is None:
        raise NotFound("Modèle inconnu")
    ensure_owns(conn, company_id, "depot", depot_id)
    depot = conn.execute("SELECT * FROM depot WHERE id = ?", (depot_id,)).fetchone()
    used = conn.execute("SELECT COUNT(*) AS n FROM vehicle WHERE depot_id = ?", (depot_id,)).fetchone()["n"]
    if used >= depot["capacity"]:
        raise GameError("Dépôt plein")
    if _cash(conn, company_id) < model["price"]:
        raise GameError("Trésorerie insuffisante")
    day = current_day(conn, company_id)
    with conn:
        cur = conn.execute(
            """INSERT INTO vehicle (company_id, model_id, depot_id, plate, km, engine_state, interior_state,
                                    revision_due_day, ct_due_day)
               VALUES (?, ?, ?, 'TMP', ?, ?, ?, ?, ?)""",
            (company_id, model_id, depot_id, model["initial_km"], model["initial_engine_state"],
             model["initial_interior_state"], day + model["initial_revision_days"],
             day + model["initial_ct_days"]),
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


# ---------- Services ----------

def create_service(conn, line_id: int, start_hour: int, end_hour: int, day_type: str = "all") -> int:
    if day_type not in rules.SERVICE_DAYS:
        raise GameError(f"Type de jour invalide ({', '.join(rules.SERVICE_DAYS)})")
    if conn.execute("SELECT 1 FROM line WHERE id = ?", (line_id,)).fetchone() is None:
        raise NotFound("Ligne introuvable")
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


def _clashes(s, others) -> bool:
    """Vrai si le service s chevauche l'un des autres services, un même type de jour."""
    days = set(rules.SERVICE_DAYS[s["day_type"]])
    return any(
        days & set(rules.SERVICE_DAYS[o["day_type"]])
        and s["start_hour"] < o["end_hour"] and o["start_hour"] < s["end_hour"]
        for o in others
    )


def _max_daily_hours(s, others) -> int:
    """Heures de conduite maximales sur un type de jour, en comptant s."""
    worst = 0
    for dtype in rules.DAY_TYPES:
        total = sum(o["end_hour"] - o["start_hour"] for o in others
                    if dtype in rules.SERVICE_DAYS[o["day_type"]])
        if dtype in rules.SERVICE_DAYS[s["day_type"]]:
            total += s["end_hour"] - s["start_hour"]
        worst = max(worst, total)
    return worst


def assign_service(conn, service_id: int, vehicle_id: int | None = None, driver_id: int | None = None) -> None:
    """Affecte un bus et/ou un conducteur à un service, en vérifiant les règles."""
    s = conn.execute(
        """SELECT s.*, l.allowed_bodies, l.company_id FROM service s
           JOIN line l ON l.id = s.line_id WHERE s.id = ?""",
        (service_id,),
    ).fetchone()
    if s is None:
        raise NotFound("Service introuvable")

    if vehicle_id is not None:
        v = _vehicle(conn, vehicle_id)
        if v["company_id"] != s["company_id"]:
            raise NotFound(f"Véhicule {vehicle_id} introuvable")
        body = conn.execute("SELECT body FROM bus_model WHERE id = ?", (v["model_id"],)).fetchone()["body"]
        if body not in s["allowed_bodies"].split(","):
            raise GameError(f"Ce type de bus ({body}) n'est pas autorisé sur cette ligne")
        others = conn.execute(
            "SELECT start_hour, end_hour, day_type FROM service WHERE vehicle_id = ? AND id != ?",
            (vehicle_id, service_id),
        ).fetchall()
        if _clashes(s, others):
            raise GameError("Ce bus a déjà un service sur cette plage horaire")

    if driver_id is not None:
        d = conn.execute("SELECT * FROM driver WHERE id = ?", (driver_id,)).fetchone()
        if d is None or d["company_id"] != s["company_id"]:
            raise NotFound(f"Conducteur {driver_id} introuvable")
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
        raise NotFound("Service introuvable")
    with conn:
        if vehicle:
            conn.execute("UPDATE service SET vehicle_id = NULL WHERE id = ?", (service_id,))
        if driver:
            conn.execute("UPDATE service SET driver_id = NULL WHERE id = ?", (service_id,))


def delete_service(conn, service_id: int) -> None:
    with conn:
        n = conn.execute("DELETE FROM service WHERE id = ?", (service_id,)).rowcount
    if n == 0:
        raise NotFound("Service introuvable")


# ---------- Atelier ----------

def _workshop_ready(conn, vehicle_id: int, cost: float):
    """Vérifie qu'un bus peut entrer à l'atelier (pas déjà immobilisé) et que la trésorerie suffit."""
    v = _vehicle(conn, vehicle_id)
    if v["status"] == "workshop":
        raise GameError(f"{v['plate']} est déjà à l'atelier")
    day = current_day(conn, v["company_id"])
    if _cash(conn, v["company_id"]) < cost:
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
    """Révision générale : état moteur remis à 100, immobilisation de OVERHAUL_DOWNTIME_DAYS jours."""
    v, day = _workshop_ready(conn, vehicle_id, rules.OVERHAUL_COST)
    with conn:
        conn.execute(
            """UPDATE vehicle SET engine_state = 100, revision_due_day = ?,
                      status = 'workshop', available_from_day = ? WHERE id = ?""",
            (day + rules.REVISION_INTERVAL_DAYS, day + rules.OVERHAUL_DOWNTIME_DAYS, vehicle_id),
        )
        post(conn, v["company_id"], day, "overhaul", -rules.OVERHAUL_COST,
             f"Révision générale {v['plate']}", vehicle_id)


def refurbish_interior(conn, vehicle_id: int) -> None:
    """Remise à neuf de l'intérieur : état remis à 100, immobilisation de REFURBISH_DOWNTIME_DAYS jours."""
    v, day = _workshop_ready(conn, vehicle_id, rules.REFURBISH_COST)
    with conn:
        conn.execute(
            "UPDATE vehicle SET interior_state = 100, status = 'workshop', available_from_day = ? WHERE id = ?",
            (day + rules.REFURBISH_DOWNTIME_DAYS, vehicle_id),
        )
        post(conn, v["company_id"], day, "refurbish", -rules.REFURBISH_COST,
             f"Remise à neuf intérieur {v['plate']}", vehicle_id)


def pass_ct(conn, vehicle_id: int) -> None:
    v = _vehicle(conn, vehicle_id)
    day = current_day(conn, v["company_id"])
    if _cash(conn, v["company_id"]) < rules.CT_COST:
        raise GameError("Trésorerie insuffisante")
    with conn:
        conn.execute(
            "UPDATE vehicle SET ct_due_day = ? WHERE id = ?", (day + rules.CT_INTERVAL_DAYS, vehicle_id)
        )
        post(conn, v["company_id"], day, "ct", -rules.CT_COST, f"Contrôle technique {v['plate']}", vehicle_id)


# ---------- Options : stock de l'atelier et installation ----------

def _option(conn, code: str):
    opt = conn.execute("SELECT * FROM option WHERE code = ?", (code,)).fetchone()
    if opt is None:
        raise NotFound(f"Option « {code} » inconnue")
    return opt


def buy_option(conn, company_id: int, option_code: str, quantity: int) -> None:
    """Achète des options pour le stock de l'atelier."""
    opt = _option(conn, option_code)
    if not 1 <= quantity <= 500:
        raise GameError("Quantité invalide (1 à 500)")
    cost = opt["price"] * quantity
    if _cash(conn, company_id) < cost:
        raise GameError("Trésorerie insuffisante")
    day = current_day(conn, company_id)
    with conn:
        conn.execute(
            """INSERT INTO option_stock (company_id, option_id, quantity) VALUES (?, ?, ?)
               ON CONFLICT (company_id, option_id) DO UPDATE SET quantity = quantity + excluded.quantity""",
            (company_id, opt["id"], quantity))
        post(conn, company_id, day, "option", -cost, f"Achat de {quantity} × {opt['name']}", opt["id"])


def option_stock(conn, company_id: int, option_code: str) -> int:
    row = conn.execute(
        """SELECT s.quantity FROM option_stock s JOIN option o ON o.id = s.option_id
           WHERE s.company_id = ? AND o.code = ?""", (company_id, option_code)).fetchone()
    return row["quantity"] if row else 0


def install_option(conn, vehicle_id: int, option_code: str) -> None:
    """Installe une option du stock sur un bus : le bus est immobilisé à l'atelier pendant install_days jours."""
    v = _vehicle(conn, vehicle_id)
    opt = _option(conn, option_code)
    if v["status"] == "workshop":
        raise GameError(f"{v['plate']} est déjà à l'atelier")
    if conn.execute("SELECT 1 FROM vehicle_option WHERE vehicle_id = ? AND option_id = ?",
                    (vehicle_id, opt["id"])).fetchone():
        raise GameError(f"{v['plate']} a déjà cette option")
    if option_stock(conn, v["company_id"], option_code) < 1:
        raise GameError(f"Plus de « {opt['name']} » en stock : achetez-en d'abord")
    day = current_day(conn, v["company_id"])
    with conn:
        conn.execute("UPDATE option_stock SET quantity = quantity - 1 WHERE company_id = ? AND option_id = ?",
                     (v["company_id"], opt["id"]))
        conn.execute("INSERT INTO vehicle_option (vehicle_id, option_id, installed_day) VALUES (?, ?, ?)",
                     (vehicle_id, opt["id"], day))
        conn.execute("UPDATE vehicle SET status = 'workshop', available_from_day = ? WHERE id = ?",
                     (day + opt["install_days"], vehicle_id))
        log_event(conn, v["company_id"], day, "install",
                  f"{opt['name']} en cours d'installation sur {v['plate']} ({opt['install_days']} j à l'atelier)",
                  vehicle_id)


# ---------- Infrastructures du dépôt : citernes et bornes ----------

def build_tank(conn, company_id: int, depot_id: int, energy: str, capacity: float) -> None:
    """Construit une citerne (ou agrandit celle qui existe) : capacity en L (diesel) ou kg (gnv)."""
    if energy not in rules.TANK_COST_PER_UNIT:
        raise GameError(f"Pas de citerne pour « {energy} » (diesel ou gnv)")
    if capacity <= 0 or capacity > 200_000:
        raise GameError("Capacité invalide")
    ensure_owns(conn, company_id, "depot", depot_id)
    cost = capacity * rules.TANK_COST_PER_UNIT[energy]
    if _cash(conn, company_id) < cost:
        raise GameError("Trésorerie insuffisante")
    day = current_day(conn, company_id)
    with conn:
        conn.execute(
            """INSERT INTO energy_infra (depot_id, energy, capacity, stock) VALUES (?, ?, ?, 0)
               ON CONFLICT (depot_id, energy) DO UPDATE SET capacity = capacity + excluded.capacity""",
            (depot_id, energy, capacity))
        post(conn, company_id, day, "infrastructure", -cost,
             f"Citerne {energy} : +{capacity:g} {'kg' if energy == 'gnv' else 'L'}", depot_id)


def build_chargers(conn, company_id: int, depot_id: int, count: int) -> None:
    """Installe des bornes de recharge (chacune recharge BUSES_PER_CHARGER bus électriques par nuit)."""
    if not 1 <= count <= 50:
        raise GameError("Nombre de bornes invalide (1 à 50)")
    ensure_owns(conn, company_id, "depot", depot_id)
    cost = rules.CHARGER_COST * count
    if _cash(conn, company_id) < cost:
        raise GameError("Trésorerie insuffisante")
    day = current_day(conn, company_id)
    with conn:
        conn.execute(
            """INSERT INTO energy_infra (depot_id, energy, capacity, stock) VALUES (?, 'electrique', ?, 0)
               ON CONFLICT (depot_id, energy) DO UPDATE SET capacity = capacity + excluded.capacity""",
            (depot_id, count))
        post(conn, company_id, day, "infrastructure", -cost, f"{count} borne(s) de recharge", depot_id)


def buy_fuel(conn, company_id: int, depot_id: int, energy: str, quantity: float) -> None:
    """Remplit une citerne au prix de base (moins cher qu'en station publique)."""
    if energy not in rules.TANK_COST_PER_UNIT:
        raise GameError(f"Pas de carburant en citerne pour « {energy} » (diesel ou gnv)")
    ensure_owns(conn, company_id, "depot", depot_id)
    tank = conn.execute("SELECT * FROM energy_infra WHERE depot_id = ? AND energy = ?",
                        (depot_id, energy)).fetchone()
    if tank is None:
        raise GameError(f"Ce dépôt n'a pas de citerne {energy} : construisez-en une")
    if quantity <= 0:
        raise GameError("Quantité invalide")
    free = tank["capacity"] - tank["stock"]
    if quantity > free + 1e-6:
        raise GameError(f"La citerne ne peut plus recevoir que {free:,.0f} {'kg' if energy == 'gnv' else 'L'}")
    cost = quantity * rules.ENERGY_PRICE[energy]
    if _cash(conn, company_id) < cost:
        raise GameError("Trésorerie insuffisante")
    day = current_day(conn, company_id)
    with conn:
        conn.execute("UPDATE energy_infra SET stock = MIN(capacity, stock + ?) WHERE id = ?",
                     (quantity, tank["id"]))
        post(conn, company_id, day, "fuel", -cost,
             f"Livraison {quantity:,.0f} {'kg' if energy == 'gnv' else 'L'} {energy}", depot_id)
