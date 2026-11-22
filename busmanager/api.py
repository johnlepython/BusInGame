"""API web (FastAPI) + service de l'interface statique.

Lancer :  uvicorn busmanager.api:app --reload      (puis http://127.0.0.1:8000)
Variables : BUSMANAGER_DB (défaut game.db), BUSMANAGER_DEV=1 active le bouton « jour suivant » (POST /api/tick).

Pour l'instant : une seule compagnie (la première de la base).
"""
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import actions, engine, rules, seed
from .db import connect, init_db

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


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


class TickIn(BaseModel):
    days: int = Field(default=1, ge=1, le=60)


def create_app(db_path: str | None = None, dev: bool | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("BUSMANAGER_DB", "game.db")
    dev = (os.environ.get("BUSMANAGER_DEV", "1") == "1") if dev is None else dev
    app = FastAPI(title="Bus Manager")

    # Crée la base et une partie au premier lancement
    boot = connect(db_path)
    init_db(boot)
    if boot.execute("SELECT 1 FROM company LIMIT 1").fetchone() is None:
        seed.new_game(boot)
    boot.close()

    def get_conn():
        conn = connect(db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    def company(conn) -> sqlite3.Row:
        return conn.execute("SELECT * FROM company ORDER BY id LIMIT 1").fetchone()

    @app.exception_handler(actions.GameError)
    async def game_error(_: Request, exc: actions.GameError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # ---------- Lecture ----------

    @app.get("/api/state")
    def state(conn=Depends(get_conn)):
        c = company(conn)
        day = c["game_day"]
        start = date.fromisoformat(c["start_date"]) if c["start_date"] else date.today()
        depots = [
            {**dict(d), "used": conn.execute(
                "SELECT COUNT(*) FROM vehicle WHERE depot_id = ?", (d["id"],)).fetchone()[0]}
            for d in conn.execute("SELECT * FROM depot WHERE company_id = ?", (c["id"],))
        ]
        last = conn.execute(
            "SELECT * FROM tick_log WHERE company_id = ? ORDER BY day DESC LIMIT 1", (c["id"],)).fetchone()
        return {
            "name": c["name"], "cash": c["cash"], "reputation": c["reputation"], "level": c["level"],
            "game_day": day, "date": (start + timedelta(days=day)).isoformat(),
            "day_type": rules.day_type(day, start), "dev": dev, "depots": depots,
            "last_day": None if last is None else {
                "day": last["day"], "profit": last["cash_after"] - last["cash_before"]},
            "rules": {
                "revision_cost": rules.REVISION_COST, "ct_cost": rules.CT_COST,
                "overhaul_cost": rules.OVERHAUL_COST, "refurbish_cost": rules.REFURBISH_COST,
                "driver_max_hours": rules.DRIVER_MAX_HOURS, "driver_daily_wage": rules.DRIVER_DAILY_WAGE,
            },
        }

    @app.get("/api/models")
    def models(conn=Depends(get_conn)):
        return [dict(r) for r in conn.execute("SELECT * FROM bus_model ORDER BY price")]

    @app.get("/api/vehicles")
    def vehicles(conn=Depends(get_conn)):
        c = company(conn)
        out = []
        for v in conn.execute(
            """SELECT v.*, m.name AS model_name, m.energy, m.body, m.capacity
               FROM vehicle v JOIN bus_model m ON m.id = v.model_id
               WHERE v.company_id = ? ORDER BY v.id""", (c["id"],)):
            d = dict(v)
            d["revision_in_days"] = v["revision_due_day"] - c["game_day"]
            d["ct_in_days"] = v["ct_due_day"] - c["game_day"]
            d["services"] = conn.execute("SELECT COUNT(*) FROM service WHERE vehicle_id = ?",
                                         (v["id"],)).fetchone()[0]
            out.append(d)
        return out

    @app.get("/api/drivers")
    def drivers(conn=Depends(get_conn)):
        c = company(conn)
        out = []
        for d in conn.execute("SELECT * FROM driver WHERE company_id = ? ORDER BY id", (c["id"],)):
            row = dict(d)
            svc = conn.execute("SELECT start_hour, end_hour, day_type FROM service WHERE driver_id = ?",
                               (d["id"],)).fetchall()
            row["services"] = len(svc)
            row["max_daily_hours"] = max(
                (sum(s["end_hour"] - s["start_hour"] for s in svc if s["day_type"] in ("all", t))
                 for t in ("weekday", "weekend")), default=0)
            out.append(row)
        return out

    @app.get("/api/lines")
    def lines(conn=Depends(get_conn)):
        c = company(conn)
        out = []
        for line in conn.execute("SELECT * FROM line WHERE company_id = ? ORDER BY id", (c["id"],)):
            row = dict(line)
            row["demand"] = {
                t: {r["hour"]: r["passengers"] for r in conn.execute(
                    "SELECT hour, passengers FROM line_demand WHERE line_id = ? AND day_type = ?",
                    (line["id"], t))}
                for t in ("weekday", "weekend")
            }
            row["services"] = [dict(s) for s in conn.execute(
                """SELECT s.*, v.plate AS plate, d.name AS driver_name
                   FROM service s LEFT JOIN vehicle v ON v.id = s.vehicle_id
                   LEFT JOIN driver d ON d.id = s.driver_id
                   WHERE s.line_id = ? ORDER BY s.start_hour, s.id""", (line["id"],))]
            out.append(row)
        return out

    @app.get("/api/report")
    def report(conn=Depends(get_conn)):
        c = company(conn)
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
                """SELECT l.code, l.name, d.demand, d.served, d.revenue, d.km FROM daily_report d
                   JOIN line l ON l.id = d.line_id WHERE d.company_id = ? AND d.day = ? ORDER BY l.id""",
                (c["id"], day))],
            "ledger": [dict(r) for r in conn.execute(
                "SELECT kind, SUM(amount) AS total FROM ledger WHERE company_id = ? AND day = ? "
                "GROUP BY kind ORDER BY total DESC", (c["id"], day))],
            "history": history,
        }

    @app.get("/api/events")
    def events(limit: int = 30, conn=Depends(get_conn)):
        c = company(conn)
        return [dict(r) for r in conn.execute(
            "SELECT * FROM event_log WHERE company_id = ? ORDER BY id DESC LIMIT ?",
            (c["id"], max(1, min(limit, 200))))]

    # ---------- Actions ----------

    @app.post("/api/vehicles", status_code=201)
    def buy(body: VehicleIn, conn=Depends(get_conn)):
        c = company(conn)
        depot_id = body.depot_id
        if depot_id is None:
            depot_id = conn.execute("SELECT id FROM depot WHERE company_id = ? ORDER BY id LIMIT 1",
                                    (c["id"],)).fetchone()["id"]
        return {"id": actions.buy_vehicle(conn, c["id"], body.model_id, depot_id)}

    @app.post("/api/vehicles/{vehicle_id}/{action}")
    def vehicle_action(vehicle_id: int, action: str, conn=Depends(get_conn)):
        fn = {"revision": actions.maintain_vehicle, "ct": actions.pass_ct,
              "overhaul": actions.overhaul_engine, "refurbish": actions.refurbish_interior}.get(action)
        if fn is None:
            raise HTTPException(404, "Action inconnue")
        fn(conn, vehicle_id)
        return {"ok": True}

    @app.post("/api/drivers", status_code=201)
    def hire(body: DriverIn, conn=Depends(get_conn)):
        c = company(conn)
        return {"id": actions.hire_driver(conn, c["id"], body.name.strip(), body.articulated_licence)}

    @app.post("/api/services", status_code=201)
    def add_service(body: ServiceIn, conn=Depends(get_conn)):
        return {"id": actions.create_service(conn, body.line_id, body.start_hour, body.end_hour, body.day_type)}

    @app.patch("/api/services/{service_id}")
    def patch_service(service_id: int, body: ServicePatch, conn=Depends(get_conn)):
        if body.unassign_vehicle or body.unassign_driver:
            actions.unassign_service(conn, service_id, vehicle=body.unassign_vehicle, driver=body.unassign_driver)
        if body.vehicle_id is not None or body.driver_id is not None:
            actions.assign_service(conn, service_id, vehicle_id=body.vehicle_id, driver_id=body.driver_id)
        return {"ok": True}

    @app.delete("/api/services/{service_id}")
    def remove_service(service_id: int, conn=Depends(get_conn)):
        actions.delete_service(conn, service_id)
        return {"ok": True}

    @app.post("/api/tick")
    def tick(body: TickIn, conn=Depends(get_conn)):
        if not dev:
            raise HTTPException(403, "Le passage manuel au jour suivant est désactivé (BUSMANAGER_DEV=0)")
        c = company(conn)
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
