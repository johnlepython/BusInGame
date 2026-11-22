"""API web (FastAPI) + service de l'interface statique.

Lancer :  uvicorn busmanager.api:app --reload      (puis http://127.0.0.1:8000)
Variables :
  BUSMANAGER_DB              fichier SQLite (défaut game.db)
  BUSMANAGER_DEV=1           active le bouton « jour suivant » (POST /api/tick) ; à mettre à 0 en production
  BUSMANAGER_SECURE_COOKIES=1  marque le cookie de session « Secure » (obligatoire derrière HTTPS)

Chaque joueur a un compte et une compagnie ; toutes les routes /api (sauf register/login) exigent une session
et ne voient que les objets de la compagnie du joueur.
"""
import math
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import actions, auth, engine, rules, seed
from .db import connect, init_db

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
COOKIE = "bm_session"


class CredentialsIn(BaseModel):
    username: str = Field(max_length=60)
    password: str = Field(max_length=200)
    company_name: str = Field(default="", max_length=60)


class VehicleIn(BaseModel):
    model_id: int
    depot_id: int | None = None


class DriverIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    articulated_licence: bool = False


class ServiceIn(BaseModel):
    line_id: int
    start_hour: int
    end_hour: int
    day_type: str = "all"


class ServicePatch(BaseModel):
    vehicle_id: int | None = None
    driver_id: int | None = None
    unassign_vehicle: bool = False
    unassign_driver: bool = False


class TankIn(BaseModel):
    depot_id: int
    energy: str
    capacity: float


class ChargersIn(BaseModel):
    depot_id: int
    count: int


class FuelIn(BaseModel):
    depot_id: int
    energy: str
    quantity: float


class OptionBuyIn(BaseModel):
    code: str
    quantity: int


class InstallIn(BaseModel):
    vehicle_id: int
    code: str


class TickIn(BaseModel):
    days: int = Field(default=1, ge=1, le=60)


def create_app(db_path: str | None = None, dev: bool | None = None, secure_cookies: bool | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("BUSMANAGER_DB", "game.db")
    dev = (os.environ.get("BUSMANAGER_DEV", "1") == "1") if dev is None else dev
    secure_cookies = (os.environ.get("BUSMANAGER_SECURE_COOKIES", "0") == "1") if secure_cookies is None \
        else secure_cookies
    app = FastAPI(title="Bus Manager")

    boot = connect(db_path)
    init_db(boot)
    boot.close()

    def get_conn():
        conn = connect(db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    def me(request: Request, conn=Depends(get_conn)) -> sqlite3.Row:
        row = auth.account_for_token(conn, request.cookies.get(COOKIE))
        if row is None:
            raise HTTPException(401, "Connexion requise")
        return row

    def company(account=Depends(me), conn=Depends(get_conn)) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM company WHERE account_id = ?", (account["id"],)).fetchone()
        if row is None:
            raise HTTPException(404, "Aucune compagnie pour ce compte")
        return row

    def set_cookie(response: Response, token: str):
        response.set_cookie(COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                            samesite="lax", secure=secure_cookies, path="/")

    @app.exception_handler(actions.NotFound)
    async def not_found(_: Request, exc: actions.NotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(actions.GameError)
    async def game_error(_: Request, exc: actions.GameError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(auth.AuthError)
    async def auth_error(_: Request, exc: auth.AuthError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # ---------- Comptes ----------

    @app.post("/api/register", status_code=201)
    def register(body: CredentialsIn, response: Response, conn=Depends(get_conn)):
        account_id = auth.create_account(conn, body.username, body.password)
        name = body.company_name.strip() or f"Compagnie de {body.username.strip()}"
        seed.new_game(conn, name=name, seed=account_id, account_id=account_id)
        set_cookie(response, auth.create_session(conn, account_id))
        return {"username": body.username.strip()}

    @app.post("/api/login")
    def login(body: CredentialsIn, response: Response, conn=Depends(get_conn)):
        try:
            account_id = auth.authenticate(conn, body.username, body.password)
        except auth.AuthError as e:
            raise HTTPException(401, str(e))
        set_cookie(response, auth.create_session(conn, account_id))
        return {"username": body.username.strip()}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, conn=Depends(get_conn)):
        auth.delete_session(conn, request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def whoami(account=Depends(me)):
        return {"username": account["username"]}

    # ---------- Lecture ----------

    @app.get("/api/state")
    def state(c=Depends(company), conn=Depends(get_conn)):
        day = c["game_day"]
        start = date.fromisoformat(c["start_date"]) if c["start_date"] else date.today()
        last = conn.execute(
            "SELECT * FROM tick_log WHERE company_id = ? ORDER BY day DESC LIMIT 1", (c["id"],)).fetchone()
        return {
            "name": c["name"], "cash": c["cash"], "reputation": c["reputation"], "level": c["level"],
            "game_day": day, "date": (start + timedelta(days=day)).isoformat(),
            "day_type": rules.day_type(day, start), "dev": dev,
            "last_day": None if last is None else {
                "day": last["day"], "profit": last["cash_after"] - last["cash_before"]},
            "rules": {
                "revision_cost": rules.REVISION_COST, "ct_cost": rules.CT_COST,
                "overhaul_cost": rules.OVERHAUL_COST, "overhaul_days": rules.OVERHAUL_DOWNTIME_DAYS,
                "refurbish_cost": rules.REFURBISH_COST, "refurbish_days": rules.REFURBISH_DOWNTIME_DAYS,
                "driver_max_hours": rules.DRIVER_MAX_HOURS, "driver_daily_wage": rules.DRIVER_DAILY_WAGE,
                "charger_cost": rules.CHARGER_COST, "buses_per_charger": rules.BUSES_PER_CHARGER,
                "tank_cost_per_unit": rules.TANK_COST_PER_UNIT, "energy_price": rules.ENERGY_PRICE,
                "external_fuel_premium": rules.EXTERNAL_FUEL_PREMIUM, "fraud_rate": rules.FRAUD_RATE,
            },
        }

    @app.get("/api/models")
    def models(conn=Depends(get_conn), account=Depends(me)):
        return [dict(r) for r in conn.execute("SELECT * FROM bus_model ORDER BY price")]

    @app.get("/api/vehicles")
    def vehicles(c=Depends(company), conn=Depends(get_conn)):
        out = []
        for v in conn.execute(
            """SELECT v.*, m.name AS model_name, m.energy, m.body, m.capacity, m.range_km
               FROM vehicle v JOIN bus_model m ON m.id = v.model_id
               WHERE v.company_id = ? ORDER BY v.id""", (c["id"],)):
            d = dict(v)
            d["revision_in_days"] = v["revision_due_day"] - c["game_day"]
            d["ct_in_days"] = v["ct_due_day"] - c["game_day"]
            d["services"] = conn.execute("SELECT COUNT(*) FROM service WHERE vehicle_id = ?",
                                         (v["id"],)).fetchone()[0]
            d["options"] = [r["code"] for r in conn.execute(
                "SELECT o.code FROM vehicle_option vo JOIN option o ON o.id = vo.option_id WHERE vo.vehicle_id = ?",
                (v["id"],))]
            out.append(d)
        return out

    @app.get("/api/drivers")
    def drivers(c=Depends(company), conn=Depends(get_conn)):
        out = []
        for d in conn.execute("SELECT * FROM driver WHERE company_id = ? ORDER BY id", (c["id"],)):
            row = dict(d)
            svc = conn.execute("SELECT start_hour, end_hour, day_type FROM service WHERE driver_id = ?",
                               (d["id"],)).fetchall()
            row["services"] = len(svc)
            row["max_daily_hours"] = max(
                (sum(s["end_hour"] - s["start_hour"] for s in svc if t in rules.SERVICE_DAYS[s["day_type"]])
                 for t in rules.DAY_TYPES), default=0)
            out.append(row)
        return out

    @app.get("/api/lines")
    def lines(c=Depends(company), conn=Depends(get_conn)):
        out = []
        for line in conn.execute("SELECT * FROM line WHERE company_id = ? ORDER BY id", (c["id"],)):
            row = dict(line)
            row["demand"] = {
                t: {r["hour"]: r["passengers"] for r in conn.execute(
                    "SELECT hour, passengers FROM line_demand WHERE line_id = ? AND day_type = ?",
                    (line["id"], t))} for t in rules.DAY_TYPES}
            freq = {t: {r["band"]: r["departures_per_hour"] for r in conn.execute(
                "SELECT band, departures_per_hour FROM line_frequency WHERE line_id = ? AND day_type = ?",
                (line["id"], t))} for t in rules.DAY_TYPES}
            row["frequency"] = freq
            per_bus = rules.departures_per_bus_hour(line["duration_min"])
            row["buses_needed"] = {t: {b: math.ceil(freq[t].get(b, 0) / per_bus - 1e-9) for b in rules.BANDS}
                                   for t in rules.DAY_TYPES}
            row["services"] = [dict(s) for s in conn.execute(
                """SELECT s.*, v.plate AS plate, d.name AS driver_name
                   FROM service s LEFT JOIN vehicle v ON v.id = s.vehicle_id
                   LEFT JOIN driver d ON d.id = s.driver_id
                   WHERE s.line_id = ? ORDER BY s.start_hour, s.id""", (line["id"],))]
            # Bus affectés par heure et par type de jour, pour comparer au besoin
            row["buses_assigned"] = {
                t: {h: sum(1 for s in row["services"] if s["vehicle_id"] and s["driver_id"]
                           and t in rules.SERVICE_DAYS[s["day_type"]] and s["start_hour"] <= h < s["end_hour"])
                    for h in range(rules.FIRST_HOUR, rules.LAST_HOUR + 1)} for t in rules.DAY_TYPES}
            row["bands"] = {t: {h: rules.band_of(t, h) for h in range(rules.FIRST_HOUR, rules.LAST_HOUR + 1)}
                            for t in rules.DAY_TYPES}
            out.append(row)
        return out

    @app.get("/api/infra")
    def infra(c=Depends(company), conn=Depends(get_conn)):
        depots = []
        for d in conn.execute("SELECT * FROM depot WHERE company_id = ? ORDER BY id", (c["id"],)):
            row = dict(d)
            row["used"] = conn.execute("SELECT COUNT(*) FROM vehicle WHERE depot_id = ?", (d["id"],)).fetchone()[0]
            row["infra"] = [dict(i) for i in conn.execute(
                "SELECT energy, capacity, stock FROM energy_infra WHERE depot_id = ? ORDER BY energy", (d["id"],))]
            row["daily_use"] = {}
            for e in conn.execute(
                """SELECT energy, AVG(quantity) AS q FROM (
                       SELECT energy, quantity FROM energy_use WHERE company_id = ? ORDER BY day DESC LIMIT 21)
                   GROUP BY energy""", (c["id"],)):
                row["daily_use"][e["energy"]] = e["q"]
            depots.append(row)
        options = []
        for o in conn.execute("SELECT * FROM option ORDER BY id"):
            row = dict(o)
            row["stock"] = actions.option_stock(conn, c["id"], o["code"])
            row["installed"] = conn.execute(
                """SELECT COUNT(*) FROM vehicle_option vo JOIN vehicle v ON v.id = vo.vehicle_id
                   WHERE vo.option_id = ? AND v.company_id = ?""", (o["id"], c["id"])).fetchone()[0]
            options.append(row)
        return {"depots": depots, "options": options}

    @app.get("/api/report")
    def report(c=Depends(company), conn=Depends(get_conn)):
        last = conn.execute(
            "SELECT * FROM tick_log WHERE company_id = ? ORDER BY day DESC LIMIT 1", (c["id"],)).fetchone()
        history = [
            {"day": r["day"], "profit": r["cash_after"] - r["cash_before"], "cash": r["cash_after"]}
            for r in conn.execute(
                "SELECT * FROM (SELECT * FROM tick_log WHERE company_id = ? ORDER BY day DESC LIMIT 30) "
                "ORDER BY day", (c["id"],))
        ]
        if last is None:
            return {"day": None, "lines": [], "ledger": [], "history": history}
        day = last["day"]
        return {
            "day": day,
            "lines": [dict(r) for r in conn.execute(
                """SELECT l.code, l.name, d.demand, d.served, d.revenue, d.fraud_loss, d.km,
                          d.target_departures, d.delivered_departures
                   FROM daily_report d JOIN line l ON l.id = d.line_id
                   WHERE d.company_id = ? AND d.day = ? ORDER BY l.id""", (c["id"], day))],
            "ledger": [dict(r) for r in conn.execute(
                "SELECT kind, SUM(amount) AS total FROM ledger WHERE company_id = ? AND day = ? "
                "GROUP BY kind ORDER BY total DESC", (c["id"], day))],
            "history": history,
        }

    @app.get("/api/events")
    def events(limit: int = 30, c=Depends(company), conn=Depends(get_conn)):
        return [dict(r) for r in conn.execute(
            "SELECT * FROM event_log WHERE company_id = ? ORDER BY id DESC LIMIT ?",
            (c["id"], max(1, min(limit, 200))))]

    # ---------- Actions ----------

    @app.post("/api/vehicles", status_code=201)
    def buy(body: VehicleIn, c=Depends(company), conn=Depends(get_conn)):
        depot_id = body.depot_id
        if depot_id is None:
            depot_id = conn.execute("SELECT id FROM depot WHERE company_id = ? ORDER BY id LIMIT 1",
                                    (c["id"],)).fetchone()["id"]
        return {"id": actions.buy_vehicle(conn, c["id"], body.model_id, depot_id)}

    @app.post("/api/vehicles/{vehicle_id}/{action}")
    def vehicle_action(vehicle_id: int, action: str, c=Depends(company), conn=Depends(get_conn)):
        fn = {"revision": actions.maintain_vehicle, "ct": actions.pass_ct,
              "overhaul": actions.overhaul_engine, "refurbish": actions.refurbish_interior}.get(action)
        if fn is None:
            raise HTTPException(404, "Action inconnue")
        actions.ensure_owns(conn, c["id"], "vehicle", vehicle_id)
        fn(conn, vehicle_id)
        return {"ok": True}

    @app.post("/api/drivers", status_code=201)
    def hire(body: DriverIn, c=Depends(company), conn=Depends(get_conn)):
        return {"id": actions.hire_driver(conn, c["id"], body.name.strip(), body.articulated_licence)}

    @app.post("/api/services", status_code=201)
    def add_service(body: ServiceIn, c=Depends(company), conn=Depends(get_conn)):
        actions.ensure_owns(conn, c["id"], "line", body.line_id)
        return {"id": actions.create_service(conn, body.line_id, body.start_hour, body.end_hour, body.day_type)}

    @app.patch("/api/services/{service_id}")
    def patch_service(service_id: int, body: ServicePatch, c=Depends(company), conn=Depends(get_conn)):
        actions.ensure_owns(conn, c["id"], "service", service_id)
        if body.vehicle_id is not None:
            actions.ensure_owns(conn, c["id"], "vehicle", body.vehicle_id)
        if body.driver_id is not None:
            actions.ensure_owns(conn, c["id"], "driver", body.driver_id)
        if body.unassign_vehicle or body.unassign_driver:
            actions.unassign_service(conn, service_id, vehicle=body.unassign_vehicle, driver=body.unassign_driver)
        if body.vehicle_id is not None or body.driver_id is not None:
            actions.assign_service(conn, service_id, vehicle_id=body.vehicle_id, driver_id=body.driver_id)
        return {"ok": True}

    @app.delete("/api/services/{service_id}")
    def remove_service(service_id: int, c=Depends(company), conn=Depends(get_conn)):
        actions.ensure_owns(conn, c["id"], "service", service_id)
        actions.delete_service(conn, service_id)
        return {"ok": True}

    @app.post("/api/infra/tank")
    def tank(body: TankIn, c=Depends(company), conn=Depends(get_conn)):
        actions.build_tank(conn, c["id"], body.depot_id, body.energy, body.capacity)
        return {"ok": True}

    @app.post("/api/infra/chargers")
    def chargers(body: ChargersIn, c=Depends(company), conn=Depends(get_conn)):
        actions.build_chargers(conn, c["id"], body.depot_id, body.count)
        return {"ok": True}

    @app.post("/api/infra/fuel")
    def fuel(body: FuelIn, c=Depends(company), conn=Depends(get_conn)):
        actions.buy_fuel(conn, c["id"], body.depot_id, body.energy, body.quantity)
        return {"ok": True}

    @app.post("/api/options/buy")
    def option_buy(body: OptionBuyIn, c=Depends(company), conn=Depends(get_conn)):
        actions.buy_option(conn, c["id"], body.code, body.quantity)
        return {"ok": True}

    @app.post("/api/options/install")
    def option_install(body: InstallIn, c=Depends(company), conn=Depends(get_conn)):
        actions.ensure_owns(conn, c["id"], "vehicle", body.vehicle_id)
        actions.install_option(conn, body.vehicle_id, body.code)
        return {"ok": True}

    @app.post("/api/tick")
    def tick(body: TickIn, c=Depends(company), conn=Depends(get_conn)):
        if not dev:
            raise HTTPException(403, "Le passage manuel au jour suivant est désactivé (BUSMANAGER_DEV=0)")
        results = engine.catch_up(conn, c["id"], c["game_day"] + body.days)
        return {"days": [{"day": r["day"], "profit": r["profit"]} for r in results]}

    # ---------- Interface statique ----------

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _default_app() -> FastAPI:
    return create_app()


# `uvicorn busmanager.api:app` : l'application n'est construite qu'à la première utilisation de l'attribut
def __getattr__(name):
    if name == "app":
        globals()["app"] = _default_app()
        return globals()["app"]
    raise AttributeError(name)
