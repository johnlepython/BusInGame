"""Constantes d'équilibrage et petites formules. Tout se règle ici."""

# ---------- Énergie ----------
# Prix de base par unité (L de diesel, kg de GNV, kWh d'électricité). C'est le prix du carburant livré en citerne.
ENERGY_PRICE = {"diesel": 1.60, "hybride": 1.60, "gnv": 1.25, "electrique": 0.22}

# Infrastructure dont dépend chaque motorisation
INFRA_OF = {"diesel": "diesel", "hybride": "diesel", "gnv": "gnv", "electrique": "electrique"}

# Sans citerne (ou citerne vide), diesel et GNV s'achètent en station publique, plus cher.
# L'électricité n'a pas d'équivalent public : un bus électrique exige une borne.
EXTERNAL_FUEL_PREMIUM = {"diesel": 1.30, "gnv": 1.40}

TANK_COST_PER_UNIT = {"diesel": 1.2, "gnv": 6.0}   # € par L (ou kg) de capacité de citerne
CHARGER_COST = 45_000.0                             # € par borne de recharge
BUSES_PER_CHARGER = 3                               # bus électriques rechargés chaque nuit par borne
LOW_TANK_RATIO = 0.15                               # alerte sous 15 % de remplissage

# ---------- Usure ----------
ENGINE_WEAR_PER_KM = 0.0025         # points d'état moteur perdus par km
INTERIOR_WEAR_PER_PASSENGER = 0.0003  # points d'état intérieur perdus par passager transporté

# ---------- Conducteurs ----------
DRIVER_DAILY_WAGE = 190.0
DRIVER_MAX_HOURS = 9                # heures de service par jour et par conducteur
MAX_SERVICE_HOURS = 9

# ---------- Dépôt (coût fixe par place et par jour) ----------
DEPOT_COST_PER_PLACE = 10.0

# ---------- Entretien réglementaire ----------
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

# ---------- Pannes ----------
REPAIR_COST_RANGE = (300, 1800)
REPAIR_DOWNTIME_RANGE = (1, 4)       # jours d'immobilisation
OVERDUE_REVISION_BREAKDOWN_FACTOR = 1.5

# ---------- Fraude et options ----------
FRAUD_RATE = 0.10                    # part des recettes perdue sur un bus sans valideur
FRAUD_PROTECTION_OPTION = "valideur"  # code de l'option qui supprime cette perte

# ---------- Réputation ----------
REPUTATION_START = 50.0
REPUTATION_INERTIA = 0.10            # part de l'écart comblée chaque jour
# Poids dans la réputation cible : passagers transportés, fréquence tenue, état du parc roulant
REPUTATION_WEIGHTS = (0.4, 0.3, 0.3)

# ---------- Calendrier ----------
DAY_TYPES = ("weekday", "saturday", "sunday")
# Types de jour qu'un service couvre
SERVICE_DAYS = {
    "all": ("weekday", "saturday", "sunday"),
    "weekday": ("weekday",),
    "saturday": ("saturday",),
    "sunday": ("sunday",),
    "weekend": ("saturday", "sunday"),
}

# Heures de service du réseau (5 h à 23 h) découpées en tranches : pointe, soir, creuse = le reste
FIRST_HOUR, LAST_HOUR = 5, 22        # dernière heure de départ incluse
BAND_HOURS = {
    "weekday":  {"pointe": (7, 8, 16, 17, 18), "soir": (19, 20, 21, 22)},
    "saturday": {"pointe": (10, 11, 15, 16, 17), "soir": (19, 20, 21, 22)},
    "sunday":   {"pointe": (10, 11, 15, 16, 17), "soir": (19, 20, 21, 22)},
}
BANDS = ("creuse", "pointe", "soir")


def band_of(day_type: str, hour: int) -> str | None:
    """Tranche horaire d'une heure (None hors des heures de service)."""
    if not FIRST_HOUR <= hour <= LAST_HOUR:
        return None
    for band, hours in BAND_HOURS[day_type].items():
        if hour in hours:
            return band
    return "creuse"


def band_hours(day_type: str, band: str) -> list[int]:
    return [h for h in range(FIRST_HOUR, LAST_HOUR + 1) if band_of(day_type, h) == band]


def day_type(game_day: int, start_date=None) -> str:
    """'weekday', 'saturday' ou 'sunday' pour un jour de jeu.

    Le jour de jeu 0 tombe à start_date : le type de jour suit donc le vrai calendrier. Sans start_date,
    le jour 0 est considéré comme un lundi.
    """
    offset = start_date.weekday() if start_date is not None else 0
    weekday = (offset + game_day) % 7  # lundi = 0
    return "saturday" if weekday == 5 else "sunday" if weekday == 6 else "weekday"


def demand_multiplier(reputation: float) -> float:
    """Réputation 50 -> x1.0 ; 0 -> x0.7 ; 100 -> x1.3."""
    return 0.7 + 0.6 * reputation / 100.0


def breakdown_probability(reliability: float, engine_state: float, revision_overdue: bool) -> float:
    p = (1.0 - reliability) * (1.0 + (100.0 - engine_state) / 25.0)
    if revision_overdue:
        p *= OVERDUE_REVISION_BREAKDOWN_FACTOR
    return min(p, 0.9)


def departures_per_bus_hour(duration_min: float) -> float:
    """Départs par heure et par sens que fournit un bus qui assure la ligne (il alterne les sens)."""
    return 30.0 / duration_min
