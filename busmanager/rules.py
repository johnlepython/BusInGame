"""Constantes d'équilibrage et petites formules. Tout se règle ici."""

# Prix de l'énergie par unité (L de diesel, kg de GNV, kWh d'électricité)
ENERGY_PRICE = {"diesel": 1.60, "gnv": 1.10, "electrique": 0.22}

# Usure
ENGINE_WEAR_PER_KM = 0.0025         # points d'état moteur perdus par km
INTERIOR_WEAR_PER_PASSENGER = 0.0003  # points d'état intérieur perdus par passager transporté

# Conducteurs
DRIVER_DAILY_WAGE = 190.0
DRIVER_MAX_HOURS = 9                # heures de service par jour et par conducteur
MAX_SERVICE_HOURS = 9

# Dépôt (coût fixe par place et par jour)
DEPOT_COST_PER_PLACE = 20.0

# Entretien réglementaire
REVISION_INTERVAL_DAYS = 90
REVISION_COST = 450.0
REVISION_ENGINE_BONUS = 25.0
CT_INTERVAL_DAYS = 365
CT_COST = 140.0

# Grosses remises en état
OVERHAUL_COST = 9000.0               # révision générale du moteur : état remis à 100
OVERHAUL_DOWNTIME_DAYS = 4
REFURBISH_COST = 2500.0              # remise à neuf de l'intérieur : état remis à 100
REFURBISH_DOWNTIME_DAYS = 2

# Pannes
REPAIR_COST_RANGE = (300, 1800)
REPAIR_DOWNTIME_RANGE = (1, 4)       # jours d'immobilisation
OVERDUE_REVISION_BREAKDOWN_FACTOR = 1.5

# Réputation -> demande
REPUTATION_START = 50.0
REPUTATION_INERTIA = 0.10            # part de l'écart comblée chaque jour


WEEKEND_WEEKDAYS = (5, 6)  # samedi et dimanche (lundi = 0, comme datetime.weekday())


def day_type(game_day: int, start_date=None) -> str:
    """'weekend' ou 'weekday' pour un jour de jeu.

    Le jour de jeu 0 tombe à start_date : le type de jour suit donc le vrai calendrier. Sans start_date,
    le jour 0 est considéré comme un lundi.
    """
    offset = start_date.weekday() if start_date is not None else 0
    return "weekend" if (offset + game_day) % 7 in WEEKEND_WEEKDAYS else "weekday"


def demand_multiplier(reputation: float) -> float:
    """Réputation 50 -> x1.0 ; 0 -> x0.7 ; 100 -> x1.3."""
    return 0.7 + 0.6 * reputation / 100.0


def breakdown_probability(reliability: float, engine_state: float, revision_overdue: bool) -> float:
    p = (1.0 - reliability) * (1.0 + (100.0 - engine_state) / 25.0)
    if revision_overdue:
        p *= OVERDUE_REVISION_BREAKDOWN_FACTOR
    return min(p, 0.9)
