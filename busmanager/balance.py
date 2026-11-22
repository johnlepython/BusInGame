"""Analyse théorique d'un réseau : combien de bus faut-il pour tenir les fréquences cibles, que rapporte chaque
ligne, en combien de temps l'investissement se rembourse-t-il ?

Hypothèses : chaque ligne est exploitée avec son bus de référence (le moins cher des modèles neufs diesel
autorisés), à la fréquence cible de chaque tranche horaire ; bus équipés de valideurs ; salaires comptés à
l'heure de service. C'est une estimation pour repérer vite un tarif, une fréquence ou une demande mal réglés ;
la simulation complète (`simulate`) la complète.
"""
import math
import sqlite3

from . import rules


def reference_model(conn: sqlite3.Connection, line):
    allowed = line["allowed_bodies"].split(",")
    marks = ",".join("?" * len(allowed))
    return conn.execute(
        f"""SELECT * FROM bus_model WHERE status = 'neuf' AND energy = 'diesel' AND body IN ({marks})
            ORDER BY price LIMIT 1""", allowed).fetchone()


def analyse_line(conn: sqlite3.Connection, line, model, day_type: str) -> dict:
    freq = {r["band"]: r["departures_per_hour"] for r in conn.execute(
        "SELECT band, departures_per_hour FROM line_frequency WHERE line_id = ? AND day_type = ?",
        (line["id"], day_type))}
    demand = {r["hour"]: r["passengers"] for r in conn.execute(
        "SELECT hour, passengers FROM line_demand WHERE line_id = ? AND day_type = ?", (line["id"], day_type))}
    trips_per_hour = 60.0 / line["duration_min"]
    km_per_bus_hour = trips_per_hour * line["distance_km"]
    price = rules.ENERGY_PRICE[model["energy"]]

    fleet = bus_hours = 0
    served = passengers = 0.0
    for band in rules.BANDS:
        buses = math.ceil(freq.get(band, 0) / rules.departures_per_bus_hour(line["duration_min"]) - 1e-9)
        fleet = max(fleet, buses)
        for hour in rules.band_hours(day_type, band):
            bus_hours += buses
            d = demand.get(hour, 0)
            passengers += d
            served += min(d, buses * model["capacity"] * trips_per_hour)

    km = bus_hours * km_per_bus_hour
    revenue = served * line["fare"]
    cost = (km * model["consumption_per_100km"] / 100.0 * price
            + km * model["maintenance_cost_per_km"]
            + bus_hours * rules.DRIVER_DAILY_WAGE / rules.DRIVER_MAX_HOURS)
    return {"passengers": passengers, "served": served, "fleet": fleet, "bus_hours": bus_hours,
            "revenue": revenue, "cost": cost, "profit": revenue - cost}


def report(conn: sqlite3.Connection, company_id: int) -> str:
    lines = conn.execute("SELECT * FROM line WHERE company_id = ? ORDER BY id", (company_id,)).fetchall()
    labels = {"weekday": "semaine", "saturday": "samedi", "sunday": "dimanche"}
    out = ["Analyse théorique : fréquences cibles tenues, bus de référence = modèle neuf diesel le moins cher "
           "autorisé sur la ligne.", "",
           f"{'Ligne':<7}{'Jour':<10}{'Pax/jour':>9}{'Servis':>8}{'Bus':>5}{'Recettes':>10}{'Coûts':>9}{'Résultat':>10}"]
    network_fleet = 0
    network_capex = 0.0
    network_week = 0.0
    for line in lines:
        model = reference_model(conn, line)
        if model is None:
            out.append(f"{line['code']:<7}aucun modèle neuf diesel autorisé dans le catalogue")
            continue
        per_day = {t: analyse_line(conn, line, model, t) for t in rules.DAY_TYPES}
        for t in rules.DAY_TYPES:
            a = per_day[t]
            out.append(f"{line['code']:<7}{labels[t]:<10}{a['passengers']:>9,.0f}{a['served']:>8,.0f}{a['fleet']:>5}"
                       f"{a['revenue']:>10,.0f}{a['cost']:>9,.0f}{a['profit']:>10,.0f}")
        week = (5 * per_day["weekday"]["profit"] + per_day["saturday"]["profit"] + per_day["sunday"]["profit"]) / 7
        fleet = max(a["fleet"] for a in per_day.values())
        capex = fleet * model["price"]
        payback = f"{capex / week:,.0f} jours" if week > 0 else "jamais"
        out.append(f"{'':<7}→ moyenne {week:,.0f} €/jour avec {fleet} × {model['brand']} {model['model']} "
                   f"({capex:,.0f} €) : retour sur investissement {payback}")
        out.append("")
        network_fleet += fleet
        network_capex += capex
        network_week += week
    fixed = conn.execute("SELECT COALESCE(SUM(capacity), 0) AS c FROM depot WHERE company_id = ?",
                         (company_id,)).fetchone()["c"] * rules.DEPOT_COST_PER_PLACE
    out.append(f"Réseau complet : {network_fleet} bus ({network_capex:,.0f} €), résultat moyen "
               f"{network_week:,.0f} €/jour avant frais fixes du dépôt ({fixed:,.0f} €/jour) → "
               f"{network_week - fixed:,.0f} €/jour.")
    out.append("Repère : un retour entre 250 et 500 jours pour le réseau complet donne un jeu progressif ; "
               "en dessous, c'est trop facile ; « jamais » signale une ligne ruineuse.")
    return "\n".join(out)
