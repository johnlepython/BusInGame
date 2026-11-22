"""Moteur du tick quotidien.

run_day() calcule UN jour pour UNE compagnie, dans une seule transaction.
catch_up() rejoue tous les jours manquants : le cron peut donc tourner en retard ou deux fois
sans risque (un jour déjà calculé n'est jamais recalculé, cf. tick_log).

Déroulé d'une journée : les bus sortent d'atelier, on détermine quels services roulent réellement,
puis on simule heure par heure (demande, capacité, autonomie des électriques), on encaisse les recettes
(moins la fraude des bus sans valideur), on règle l'énergie (citerne du dépôt, sinon station publique),
l'entretien et les salaires, on applique l'usure et les pannes, puis on met à jour la réputation.

Simplifications :
- les pannes sont tirées en fin de journée (le bus est indisponible à partir du lendemain) ;
- l'énergie est prélevée en fin de journée sur la citerne du dépôt du bus ;
- les bus électriques repartent chaque matin avec une batterie pleine, s'ils ont une place de borne.
"""
import random
import sqlite3
from collections import defaultdict
from datetime import date

from . import rules
from .actions import log_event, post


class AlreadyTicked(Exception):
    pass


def _line_context(conn, line, dtype):
    freq = {r["band"]: r["departures_per_hour"] for r in conn.execute(
        "SELECT band, departures_per_hour FROM line_frequency WHERE line_id = ? AND day_type = ?",
        (line["id"], dtype))}
    demand = {r["hour"]: r["passengers"] for r in conn.execute(
        "SELECT hour, passengers FROM line_demand WHERE line_id = ? AND day_type = ?", (line["id"], dtype))}
    services = [s for s in conn.execute("SELECT * FROM service WHERE line_id = ?", (line["id"],))
                if dtype in rules.SERVICE_DAYS[s["day_type"]]]
    return {"freq": freq, "demand": demand, "services": services,
            "allowed": set(line["allowed_bodies"].split(","))}


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
            (company_id, day))

        vehicles = {
            v["id"]: v for v in conn.execute(
                """SELECT v.*, m.capacity, m.body, m.energy, m.consumption_per_100km, m.range_km,
                          m.maintenance_cost_per_km, m.reliability
                   FROM vehicle v JOIN bus_model m ON m.id = v.model_id
                   WHERE v.company_id = ? ORDER BY v.id""", (company_id,))
        }
        protected = {r["vehicle_id"] for r in conn.execute(
            """SELECT vo.vehicle_id FROM vehicle_option vo JOIN option o ON o.id = vo.option_id
               JOIN vehicle v ON v.id = vo.vehicle_id
               WHERE v.company_id = ? AND o.code = ?""", (company_id, rules.FRAUD_PROTECTION_OPTION))}
        drivers = {d["id"]: d for d in conn.execute("SELECT * FROM driver WHERE company_id = ?", (company_id,))}
        lines = conn.execute("SELECT * FROM line WHERE company_id = ? ORDER BY id", (company_id,)).fetchall()
        tanks = {(t["depot_id"], t["energy"]): dict(t) for t in conn.execute(
            """SELECT i.* FROM energy_infra i JOIN depot d ON d.id = i.depot_id WHERE d.company_id = ?""",
            (company_id,))}

        # 2. Services réellement opérables aujourd'hui
        ctx = {line["id"]: _line_context(conn, line, dtype) for line in lines}
        for line in lines:
            c = ctx[line["id"]]
            ok = []
            for s in c["services"]:
                v = vehicles.get(s["vehicle_id"])
                d = drivers.get(s["driver_id"])
                if v is None or d is None or v["status"] != "active" or v["ct_due_day"] < day:
                    continue
                if v["body"] not in c["allowed"]:
                    continue
                if v["body"] == "articule" and not d["articulated_licence"]:
                    continue
                ok.append(s)
            c["services"] = ok

        # Bus électriques : une place de borne chacun (BUSES_PER_CHARGER bus par borne et par dépôt)
        no_charger = set()
        electric_by_depot = defaultdict(set)
        for c in ctx.values():
            for s in c["services"]:
                if vehicles[s["vehicle_id"]]["energy"] == "electrique":
                    electric_by_depot[vehicles[s["vehicle_id"]]["depot_id"]].add(s["vehicle_id"])
        for depot_id, vids in electric_by_depot.items():
            chargers = int(tanks.get((depot_id, "electrique"), {}).get("capacity", 0))
            slots = chargers * rules.BUSES_PER_CHARGER
            for vid in sorted(vids)[slots:]:
                no_charger.add(vid)
                log_event(conn, company_id, day, "no_charger",
                          f"{vehicles[vid]['plate']} (électrique) n'a pas de place de borne : il ne roule pas. "
                          f"Installez des bornes au dépôt.", vid)
        for c in ctx.values():
            c["services"] = [s for s in c["services"] if s["vehicle_id"] not in no_charger]

        # 3. Simulation heure par heure
        v_km = defaultdict(float)
        v_pax = defaultdict(float)
        exhausted = set()
        stats = {line["id"]: defaultdict(float) for line in lines}

        for hour in range(24):
            band = rules.band_of(dtype, hour)
            for line in lines:
                c, st = ctx[line["id"]], stats[line["id"]]
                demand = c["demand"].get(hour, 0) * mult
                st["demand"] += demand
                trips_per_hour = 60.0 / line["duration_min"]
                km_hour = trips_per_hour * line["distance_km"]
                active = []
                for s in c["services"]:
                    if not s["start_hour"] <= hour < s["end_hour"]:
                        continue
                    vid = s["vehicle_id"]
                    range_km = vehicles[vid]["range_km"]
                    if range_km is not None and v_km[vid] + km_hour > range_km + 1e-9:
                        exhausted.add(vid)  # batterie vide : le bus ne finit pas son service
                        continue
                    active.append(s)

                target = c["freq"].get(band, 0.0) if band else 0.0
                delivered = len(active) * rules.departures_per_bus_hour(line["duration_min"])
                st["target"] += target
                st["delivered"] += min(delivered, target)
                if not active:
                    continue

                caps = {s["id"]: vehicles[s["vehicle_id"]]["capacity"] * trips_per_hour for s in active}
                total_cap = sum(caps.values())
                served = min(demand, total_cap)
                st["served"] += served
                for s in active:
                    vid = s["vehicle_id"]
                    pax = served * caps[s["id"]] / total_cap
                    v_pax[vid] += pax
                    v_km[vid] += km_hour
                    st["km"] += km_hour
                    gross = pax * line["fare"]
                    fraud = 0.0 if vid in protected else gross * rules.FRAUD_RATE
                    st["revenue"] += gross - fraud
                    st["fraud"] += fraud

        for vid in sorted(exhausted):
            log_event(conn, company_id, day, "battery",
                      f"{vehicles[vid]['plate']} : batterie vide avant la fin de ses services "
                      f"(autonomie {vehicles[vid]['range_km']:g} km).", vid)

        # 4. Recettes et rapport par ligne
        total_demand = total_served = total_target = total_delivered = 0.0
        for line in lines:
            st = stats[line["id"]]
            total_demand += st["demand"]
            total_served += st["served"]
            total_target += st["target"]
            total_delivered += st["delivered"]
            if st["revenue"]:
                post(conn, company_id, day, "revenue", st["revenue"], f"Recettes ligne {line['code']}", line["id"])
            conn.execute(
                """INSERT INTO daily_report (company_id, day, line_id, demand, served, revenue, fraud_loss, km,
                                             target_departures, delivered_departures)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (company_id, day, line["id"], st["demand"], st["served"], st["revenue"], st["fraud"],
                 st["km"], st["target"], st["delivered"]))

        # 5. Énergie, entretien courant, usure, pannes (par bus ayant roulé)
        external_bought = defaultdict(float)
        used_energy = defaultdict(float)
        for vid in sorted(v_km):
            v, km = vehicles[vid], v_km[vid]
            need = km * v["consumption_per_100km"] / 100.0
            infra = rules.INFRA_OF[v["energy"]]
            price = rules.ENERGY_PRICE[v["energy"]]
            used_energy[infra] += need
            if infra == "electrique":
                post(conn, company_id, day, "fuel", -need * price, f"Électricité {v['plate']}", vid)
            else:
                tank = tanks.get((v["depot_id"], infra))
                taken = min(tank["stock"], need) if tank else 0.0
                if tank:
                    tank["stock"] -= taken
                external = need - taken
                if external > 1e-9:
                    external_bought[infra] += external
                    post(conn, company_id, day, "fuel",
                         -external * price * rules.EXTERNAL_FUEL_PREMIUM[infra],
                         f"Carburant en station {v['plate']}", vid)
            wear_factor = 1.0 + (100.0 - v["engine_state"]) / 100.0
            post(conn, company_id, day, "maintenance",
                 -km * v["maintenance_cost_per_km"] * wear_factor, f"Entretien courant {v['plate']}", vid)

            engine_state = max(0.0, v["engine_state"] - km * rules.ENGINE_WEAR_PER_KM)
            interior = max(0.0, v["interior_state"] - v_pax[vid] * rules.INTERIOR_WEAR_PER_PASSENGER)
            conn.execute(
                "UPDATE vehicle SET km = km + ?, engine_state = ?, interior_state = ? WHERE id = ?",
                (km, engine_state, interior, vid))

            p = rules.breakdown_probability(v["reliability"], engine_state, day > v["revision_due_day"])
            if rng.random() < p:
                cost = rng.randint(*rules.REPAIR_COST_RANGE)
                downtime = rng.randint(*rules.REPAIR_DOWNTIME_RANGE)
                conn.execute("UPDATE vehicle SET status = 'workshop', available_from_day = ? WHERE id = ?",
                             (day + 1 + downtime, vid))
                post(conn, company_id, day, "repair", -cost, f"Panne {v['plate']}", vid)
                log_event(conn, company_id, day, "breakdown",
                          f"Panne de {v['plate']} : {downtime} j d'immobilisation, {cost} €", vid)

        for energy, quantity in used_energy.items():
            conn.execute("INSERT INTO energy_use (company_id, day, energy, quantity) VALUES (?, ?, ?, ?)",
                         (company_id, day, energy, quantity))

        # Citernes : enregistrement du stock restant et alertes
        for (depot_id, energy), tank in tanks.items():
            if energy == "electrique":
                continue
            conn.execute("UPDATE energy_infra SET stock = ? WHERE id = ?", (max(0.0, tank["stock"]), tank["id"]))
            unit = "kg" if energy == "gnv" else "L"
            if external_bought.get(energy):
                log_event(conn, company_id, day, "station",
                          f"Citerne {energy} vide : {external_bought[energy]:,.0f} {unit} achetés en station "
                          f"(+{(rules.EXTERNAL_FUEL_PREMIUM[energy] - 1) * 100:.0f} %).")
            elif tank["capacity"] > 0 and tank["stock"] / tank["capacity"] < rules.LOW_TANK_RATIO:
                log_event(conn, company_id, day, "low_fuel",
                          f"Citerne {energy} presque vide ({tank['stock']:,.0f} {unit} restants).")
        for energy in ("diesel", "gnv"):
            if external_bought.get(energy) and not any(e == energy for (_, e) in tanks):
                log_event(conn, company_id, day, "station",
                          f"Pas de citerne {energy} : {external_bought[energy]:,.0f} "
                          f"{'kg' if energy == 'gnv' else 'L'} achetés en station (+"
                          f"{(rules.EXTERNAL_FUEL_PREMIUM[energy] - 1) * 100:.0f} %).")

        # 6. Alertes d'échéances
        for v in vehicles.values():
            if v["revision_due_day"] == day:
                log_event(conn, company_id, day, "revision_due", f"Révision due pour {v['plate']}", v["id"])
            if v["ct_due_day"] == day:
                log_event(conn, company_id, day, "ct_due",
                          f"Contrôle technique de {v['plate']} expire : il ne roulera plus après aujourd'hui",
                          v["id"])

        # 7. Frais fixes : salaires (tous les conducteurs) et dépôts
        for d in drivers.values():
            post(conn, company_id, day, "wages", -d["daily_wage"], f"Salaire {d['name']}", d["id"])
        for dep in conn.execute("SELECT * FROM depot WHERE company_id = ?", (company_id,)):
            post(conn, company_id, day, "depot", -dep["capacity"] * rules.DEPOT_COST_PER_PLACE,
                 f"Dépôt {dep['name']}", dep["id"])

        # 8. Réputation : passagers transportés, fréquence tenue, état du parc roulant
        if total_demand > 0:
            served_ratio = total_served / total_demand
            frequency_ratio = total_delivered / total_target if total_target > 0 else 1.0
            states = []
            for vid in v_km:
                row = conn.execute("SELECT engine_state, interior_state FROM vehicle WHERE id = ?",
                                   (vid,)).fetchone()
                states.append((row["engine_state"] + row["interior_state"]) / 2.0)
            state_ratio = sum(states) / len(states) / 100.0 if states else 0.0
            w_served, w_freq, w_state = rules.REPUTATION_WEIGHTS
            target_rep = 100.0 * (w_served * served_ratio + w_freq * frequency_ratio + w_state * state_ratio)
            new_rep = company["reputation"] + rules.REPUTATION_INERTIA * (target_rep - company["reputation"])
            conn.execute("UPDATE company SET reputation = ? WHERE id = ?", (new_rep, company_id))

        # 9. Clôture du jour
        cash_after = conn.execute("SELECT cash FROM company WHERE id = ?", (company_id,)).fetchone()["cash"]
        conn.execute(
            "INSERT INTO tick_log (company_id, day, cash_before, cash_after) VALUES (?, ?, ?, ?)",
            (company_id, day, cash_before, cash_after))
        conn.execute("UPDATE company SET game_day = game_day + 1 WHERE id = ?", (company_id,))

    return {
        "day": day,
        "day_type": dtype,
        "cash_before": cash_before,
        "cash_after": cash_after,
        "profit": cash_after - cash_before,
        "demand": total_demand,
        "served": total_served,
        "target_departures": total_target,
        "delivered_departures": total_delivered,
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


def catch_up_all(conn: sqlite3.Connection, today) -> dict[int, list[dict]]:
    """Rattrape toutes les compagnies jusqu'à la date donnée (tâche planifiée quotidienne)."""
    out = {}
    for row in conn.execute("SELECT id FROM company WHERE start_date IS NOT NULL ORDER BY id").fetchall():
        out[row["id"]] = catch_up(conn, row["id"], target_day_for_date(conn, row["id"], today))
    return out
