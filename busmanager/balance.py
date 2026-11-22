"""Analyse théorique d'une ligne : combien de bus faut-il, que rapporte-t-elle, en combien de temps se rembourse-t-elle ?

C'est une estimation (passagers entièrement servis, bus employés aux heures où ils sont utiles),
destinée à repérer vite une ligne ou un tarif mal réglé avant de lancer une simulation complète.
"""
import math
import sqlite3

from . import rules


def analyse_line(conn: sqlite3.Connection, line, model, day_type: str) -> dict:
    trips_per_hour = 60.0 / line["duration_min"]
    cap_per_bus_hour = model["capacity"] * trips_per_hour
    km_per_bus_hour = trips_per_hour * line["distance_km"]
    demand = {
        r["hour"]: r["passengers"]
        for r in conn.execute(
            "SELECT hour, passengers FROM line_demand WHERE line_id = ? AND day_type = ?",
            (line["id"], day_type),
        )
    }
    buses_by_hour = {h: math.ceil(p / cap_per_bus_hour) for h, p in demand.items() if p > 0}
    bus_hours = sum(buses_by_hour.values())
    fleet = max(buses_by_hour.values(), default=0)
    passengers = sum(demand.values())
    revenue = passengers * line["fare"]

    km = bus_hours * km_per_bus_hour
    fuel = km * model["consumption_per_100km"] / 100.0 * rules.ENERGY_PRICE[model["energy"]]
    maint = km * model["maintenance_cost_per_km"]
    wages = bus_hours * rules.DRIVER_DAILY_WAGE / rules.DRIVER_MAX_HOURS
    cost = fuel + maint + wages
    profit = revenue - cost
    capex = fleet * model["price"]
    return {
        "passengers": passengers, "peak_hour": max(demand.values(), default=0),
        "fleet": fleet, "bus_hours": bus_hours, "revenue": revenue, "cost": cost, "profit": profit,
        "payback_days": capex / profit if profit > 0 else None,
        "bus_cap_per_hour": cap_per_bus_hour,
    }


def report(conn: sqlite3.Connection, company_id: int) -> str:
    lines = conn.execute("SELECT * FROM line WHERE company_id = ? ORDER BY id", (company_id,)).fetchall()
    cheapest = conn.execute("SELECT * FROM bus_model WHERE body = 'standard' ORDER BY price LIMIT 1").fetchone()
    out = [f"Analyse théorique avec le modèle « {cheapest['name']} » ({cheapest['capacity']} places, "
           f"{cheapest['price']:,.0f} €). Passagers entièrement servis ; salaires comptés à l'heure de service.",
           ""]
    header = (f"{'Ligne':<6}{'Jour':<9}{'Pax/jour':>9}{'Pointe/h':>9}{'Cap bus/h':>10}{'Bus':>5}"
              f"{'Recettes':>10}{'Coûts':>9}{'Résultat':>10}{'Retour (j)':>12}")
    out.append(header)
    for line in lines:
        for dtype, label in (("weekday", "semaine"), ("weekend", "week-end")):
            a = analyse_line(conn, line, cheapest, dtype)
            payback = f"{a['payback_days']:.0f}" if a["payback_days"] else "jamais"
            out.append(f"{line['code']:<6}{label:<9}{a['passengers']:>9,.0f}{a['peak_hour']:>9,.0f}"
                       f"{a['bus_cap_per_hour']:>10,.0f}{a['fleet']:>5}{a['revenue']:>10,.0f}"
                       f"{a['cost']:>9,.0f}{a['profit']:>10,.0f}{payback:>12}")
    fixed = conn.execute("SELECT COALESCE(SUM(capacity), 0) AS c FROM depot WHERE company_id = ?",
                         (company_id,)).fetchone()["c"] * rules.DEPOT_COST_PER_PLACE
    out.append("")
    out.append(f"Frais fixes du dépôt : {fixed:,.0f} €/jour. Un retour sur investissement "
               f"entre 100 et 300 jours donne un jeu intéressant ; moins = trop facile, « jamais » = ligne ruineuse.")
    return "\n".join(out)
