"""Moteur du tick quotidien.

run_day() calcule UN jour pour UNE compagnie, dans une seule transaction.
catch_up() rejoue tous les jours manquants : le cron peut donc tourner en retard ou deux fois
sans risque (un jour déjà calculé n'est jamais recalculé, cf. tick_log).

Simplifications du niveau 1 :
- les pannes sont tirées en fin de journée (le bus est indisponible à partir du lendemain) ;
- un bus assigné à plusieurs services le même jour ne peut pas les chevaucher (vérifié à l'affectation).
"""
import random
import sqlite3
from collections import defaultdict
from datetime import date

from . import rules
from .actions import log_event, post


class AlreadyTicked(Exception):
    pass


def run_day(conn: sqlite3.Connection, company_id: int) -> dict:
    company = conn.execute("SELECT * FROM company WHERE id = ?", (company_id,)).fetchone()
    day = company["game_day"]
    if conn.execute(
        "SELECT 1 FROM tick_log WHERE company_id = ? AND day = ?", (company_id, day)
    ).fetchone():
        raise AlreadyTicked(f"Jour {day} déjà calculé pour la compagnie {company_id}")

    cash_before = company["cash"]
    start = date.fromisoformat(company["start_date"]) if company["start_date"] else None
    dtype = rules.day_type(day, start)
    mult = rules.demand_multiplier(company["reputation"])
    rng = random.Random(f"{company['seed']}:{company_id}:{day}")

    with conn:
        # 1. Retour d'atelier
        conn.execute(
            "UPDATE vehicle SET status = 'active' "
            "WHERE company_id = ? AND status = 'workshop' AND available_from_day <= ?",
            (company_id, day),
        )

        vehicles = {
            v["id"]: v
            for v in conn.execute(
                """SELECT v.*, m.capacity, m.body, m.energy, m.consumption_per_100km,
                          m.maintenance_cost_per_km, m.reliability
                   FROM vehicle v JOIN bus_model m ON m.id = v.model_id
                   WHERE v.company_id = ? ORDER BY v.id""",
                (company_id,),
            )
        }
        drivers = {d["id"]: d for d in conn.execute("SELECT * FROM driver WHERE company_id = ?", (company_id,))}
        lines = conn.execute("SELECT * FROM line WHERE company_id = ? ORDER BY id", (company_id,)).fetchall()

        # 2. Services réellement opérés aujourd'hui
        v_km = defaultdict(float)
        v_pax = defaultdict(float)
        report = {}
        total_demand = 0.0
        total_served = 0.0

        for line in lines:
            demand_by_hour = {
                r["hour"]: r["passengers"]
                for r in conn.execute(
                    "SELECT hour, passengers FROM line_demand WHERE line_id = ? AND day_type = ?",
                    (line["id"], dtype),
                )
            }
            services = conn.execute(
                "SELECT * FROM service WHERE line_id = ? AND day_type IN ('all', ?)", (line["id"], dtype)
            ).fetchall()
            operated = []
            for s in services:
                v = vehicles.get(s["vehicle_id"])
                d = drivers.get(s["driver_id"])
                if v is None or d is None:
                    continue
                if v["status"] != "active" or v["ct_due_day"] < day:
                    continue
                if line["allowed_body"] == "standard" and v["body"] == "articule":
                    continue
                if v["body"] == "articule" and not d["articulated_licence"]:
                    continue
                operated.append(s)

            trips_per_hour = 60.0 / line["duration_min"]
            line_demand = line_served = line_km = 0.0
            for hour in range(24):
                demand = demand_by_hour.get(hour, 0) * mult
                line_demand += demand
                active = [s for s in operated if s["start_hour"] <= hour < s["end_hour"]]
                if not active:
                    continue
                caps = {s["id"]: vehicles[s["vehicle_id"]]["capacity"] * trips_per_hour for s in active}
                total_cap = sum(caps.values())
                served = min(demand, total_cap)
                line_served += served
                for s in active:
                    vid = s["vehicle_id"]
                    v_pax[vid] += served * caps[s["id"]] / total_cap
                    km = trips_per_hour * line["distance_km"]
                    v_km[vid] += km
                    line_km += km

            revenue = line_served * line["fare"]
            report[line["id"]] = (line_demand, line_served, revenue, line_km)
            total_demand += line_demand
            total_served += line_served
            if revenue:
                post(conn, company_id, day, "revenue", revenue, f"Recettes {line['code']}", line["id"])
            conn.execute(
                "INSERT INTO daily_report (company_id, day, line_id, demand, served, revenue, km) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (company_id, day, line["id"], line_demand, line_served, revenue, line_km),
            )

        # 3. Coûts d'exploitation par véhicule + usure + pannes
        for vid in sorted(v_km):
            v = vehicles[vid]
            km = v_km[vid]
            fuel = km * v["consumption_per_100km"] / 100.0 * rules.ENERGY_PRICE[v["energy"]]
            wear_factor = 1.0 + (100.0 - v["engine_state"]) / 100.0
            maint = km * v["maintenance_cost_per_km"] * wear_factor
            post(conn, company_id, day, "fuel", -fuel, f"Énergie {v['plate']}", vid)
            post(conn, company_id, day, "maintenance", -maint, f"Entretien courant {v['plate']}", vid)

            engine = max(0.0, v["engine_state"] - km * rules.ENGINE_WEAR_PER_KM)
            interior = max(0.0, v["interior_state"] - v_pax[vid] * rules.INTERIOR_WEAR_PER_PASSENGER)
            conn.execute(
                "UPDATE vehicle SET km = km + ?, engine_state = ?, interior_state = ? WHERE id = ?",
                (km, engine, interior, vid),
            )

            p = rules.breakdown_probability(v["reliability"], engine, day > v["revision_due_day"])
            if rng.random() < p:
                cost = rng.randint(*rules.REPAIR_COST_RANGE)
                downtime = rng.randint(*rules.REPAIR_DOWNTIME_RANGE)
                conn.execute(
                    "UPDATE vehicle SET status = 'workshop', available_from_day = ? WHERE id = ?",
                    (day + 1 + downtime, vid),
                )
                post(conn, company_id, day, "repair", -cost, f"Panne {v['plate']}", vid)
                log_event(conn, company_id, day, "breakdown",
                          f"Panne de {v['plate']} : {downtime} j d'immobilisation, {cost} €", vid)

        # 4. Alertes d'échéances
        for v in vehicles.values():
            if v["revision_due_day"] == day:
                log_event(conn, company_id, day, "revision_due", f"Révision due pour {v['plate']}", v["id"])
            if v["ct_due_day"] == day:
                log_event(conn, company_id, day, "ct_due",
                          f"Contrôle technique de {v['plate']} expire : il ne roulera plus après aujourd'hui", v["id"])

        # 5. Frais fixes : salaires (tous les conducteurs) et dépôts
        for d in drivers.values():
            post(conn, company_id, day, "wages", -d["daily_wage"], f"Salaire {d['name']}", d["id"])
        for dep in conn.execute("SELECT * FROM depot WHERE company_id = ?", (company_id,)):
            post(conn, company_id, day, "depot", -dep["capacity"] * rules.DEPOT_COST_PER_PLACE,
                 f"Dépôt {dep['name']}", dep["id"])

        # 6. Réputation : qualité de service + état du parc roulant
        if total_demand > 0:
            served_ratio = total_served / total_demand
            running = [vehicles[i] for i in v_km]
            if running:
                states = []
                for v in running:
                    row = conn.execute(
                        "SELECT engine_state, interior_state FROM vehicle WHERE id = ?", (v["id"],)
                    ).fetchone()
                    states.append((row["engine_state"] + row["interior_state"]) / 2.0)
                state_ratio = sum(states) / len(states) / 100.0
            else:
                state_ratio = 0.0
            target = 100.0 * (0.6 * served_ratio + 0.4 * state_ratio)
            new_rep = company["reputation"] + rules.REPUTATION_INERTIA * (target - company["reputation"])
            conn.execute("UPDATE company SET reputation = ? WHERE id = ?", (new_rep, company_id))

        # 7. Clôture du jour
        cash_after = conn.execute("SELECT cash FROM company WHERE id = ?", (company_id,)).fetchone()["cash"]
        conn.execute(
            "INSERT INTO tick_log (company_id, day, cash_before, cash_after) VALUES (?, ?, ?, ?)",
            (company_id, day, cash_before, cash_after),
        )
        conn.execute("UPDATE company SET game_day = game_day + 1 WHERE id = ?", (company_id,))

    return {
        "day": day,
        "cash_before": cash_before,
        "cash_after": cash_after,
        "profit": cash_after - cash_before,
        "demand": total_demand,
        "served": total_served,
    }


def target_day_for_date(conn: sqlite3.Connection, company_id: int, today) -> int:
    """Numéro de jour de jeu « à rattraper » pour une date réelle : tous les jours < cible sont calculés.

    Le jour 0 correspond à company.start_date ; un cron lancé le soir du jour J calcule le jour J inclus.
    """
    start = conn.execute("SELECT start_date FROM company WHERE id = ?", (company_id,)).fetchone()["start_date"]
    if start is None:
        raise ValueError("Cette compagnie n'a pas de start_date")
    return (today - date.fromisoformat(start)).days + 1


def catch_up(conn: sqlite3.Connection, company_id: int, target_day: int) -> list[dict]:
    """Calcule tous les jours manquants jusqu'à target_day (exclu). Sans effet si déjà à jour."""
    results = []
    while True:
        day = conn.execute("SELECT game_day FROM company WHERE id = ?", (company_id,)).fetchone()["game_day"]
        if day >= target_day:
            return results
        results.append(run_day(conn, company_id))
