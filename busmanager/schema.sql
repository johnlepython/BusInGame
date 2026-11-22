-- Schéma du jeu. SQLite.
-- Invariant : company.cash = somme des écritures du ledger de la compagnie.

CREATE TABLE IF NOT EXISTS account (
    id            INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Le jeton envoyé au navigateur n'est jamais stocké : seule son empreinte SHA-256 l'est.
CREATE TABLE IF NOT EXISTS session (
    token_hash  TEXT    PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    expires_at  TEXT    NOT NULL                 -- horodatage UTC ISO 8601
);

CREATE TABLE IF NOT EXISTS company (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER UNIQUE REFERENCES account(id) ON DELETE CASCADE,  -- NULL : compagnie sans joueur (CLI, simulation)
    name        TEXT    NOT NULL,
    cash        REAL    NOT NULL DEFAULT 0,
    game_day    INTEGER NOT NULL DEFAULT 0,   -- prochain jour à simuler
    reputation  REAL    NOT NULL DEFAULT 50,  -- 0..100
    level       INTEGER NOT NULL DEFAULT 1,   -- niveau de jeu
    seed        INTEGER NOT NULL DEFAULT 1,   -- graine du hasard (pannes), pour la reproductibilité
    start_date  TEXT                          -- date réelle (ISO) du jour de jeu 0
);

-- Catalogue : une ligne par couple (modèle, motorisation), partagé par toutes les compagnies
CREATE TABLE IF NOT EXISTS bus_model (
    id                      INTEGER PRIMARY KEY,
    brand                   TEXT    NOT NULL,
    model                   TEXT    NOT NULL,
    name                    TEXT    NOT NULL UNIQUE,
    status                  TEXT    NOT NULL CHECK (status IN ('neuf', 'occasion')),
    body                    TEXT    NOT NULL CHECK (body IN ('standard', 'articule', 'midi')),
    energy                  TEXT    NOT NULL CHECK (energy IN ('diesel', 'gnv', 'hybride', 'electrique')),
    capacity                INTEGER NOT NULL,
    consumption_per_100km   REAL    NOT NULL,  -- L (diesel, hybride), kg (gnv), kWh (électrique)
    range_km                REAL,              -- autonomie (électriques uniquement)
    price                   REAL    NOT NULL,
    reliability             REAL    NOT NULL,  -- 0..1, plus haut = moins de pannes
    maintenance_cost_per_km REAL    NOT NULL,
    initial_km              REAL    NOT NULL DEFAULT 0,
    initial_engine_state    REAL    NOT NULL DEFAULT 100,
    initial_interior_state  REAL    NOT NULL DEFAULT 100,
    initial_ct_days         INTEGER NOT NULL DEFAULT 365,    -- validité du CT à l'achat
    initial_revision_days   INTEGER NOT NULL DEFAULT 90      -- délai avant la prochaine révision à l'achat
);

CREATE TABLE IF NOT EXISTS depot (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    capacity    INTEGER NOT NULL               -- nombre de places de bus
);

-- Citernes (diesel, gnv) et bornes de recharge (electrique) d'un dépôt.
-- diesel/gnv : capacity = volume de la citerne (L ou kg), stock = contenu actuel.
-- electrique : capacity = nombre de bornes, stock inutilisé.
CREATE TABLE IF NOT EXISTS energy_infra (
    id          INTEGER PRIMARY KEY,
    depot_id    INTEGER NOT NULL REFERENCES depot(id) ON DELETE CASCADE,
    energy      TEXT    NOT NULL CHECK (energy IN ('diesel', 'gnv', 'electrique')),
    capacity    REAL    NOT NULL DEFAULT 0,
    stock       REAL    NOT NULL DEFAULT 0,
    UNIQUE (depot_id, energy)
);

CREATE TABLE IF NOT EXISTS vehicle (
    id                 INTEGER PRIMARY KEY,
    company_id         INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    model_id           INTEGER NOT NULL REFERENCES bus_model(id),
    depot_id           INTEGER NOT NULL REFERENCES depot(id),
    plate              TEXT    NOT NULL UNIQUE,
    km                 REAL    NOT NULL DEFAULT 0,
    engine_state       REAL    NOT NULL DEFAULT 100,  -- 0..100
    interior_state     REAL    NOT NULL DEFAULT 100,  -- 0..100
    revision_due_day   INTEGER NOT NULL,
    ct_due_day         INTEGER NOT NULL,              -- contrôle technique
    status             TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'workshop')),
    available_from_day INTEGER NOT NULL DEFAULT 0     -- si 'workshop' : jour de retour en service
);

-- Catalogue des options installables sur un bus
CREATE TABLE IF NOT EXISTS option (
    id           INTEGER PRIMARY KEY,
    code         TEXT    NOT NULL UNIQUE,
    name         TEXT    NOT NULL,
    price        REAL    NOT NULL,
    install_days INTEGER NOT NULL DEFAULT 1,   -- immobilisation du bus à l'atelier pendant l'installation
    description  TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS option_stock (
    company_id  INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    option_id   INTEGER NOT NULL REFERENCES option(id),
    quantity    INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    PRIMARY KEY (company_id, option_id)
);

CREATE TABLE IF NOT EXISTS vehicle_option (
    vehicle_id    INTEGER NOT NULL REFERENCES vehicle(id) ON DELETE CASCADE,
    option_id     INTEGER NOT NULL REFERENCES option(id),
    installed_day INTEGER NOT NULL,
    PRIMARY KEY (vehicle_id, option_id)
);

CREATE TABLE IF NOT EXISTS driver (
    id                  INTEGER PRIMARY KEY,
    company_id          INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    name                TEXT    NOT NULL,
    daily_wage          REAL    NOT NULL,
    articulated_licence INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS line (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    code          TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    terminus_a    TEXT    NOT NULL DEFAULT '',
    terminus_b    TEXT    NOT NULL DEFAULT '',
    distance_km   REAL    NOT NULL,   -- distance d'un trajet (sens unique)
    duration_min  REAL    NOT NULL,   -- durée d'un trajet (sens unique)
    allowed_bodies TEXT   NOT NULL,   -- ex. 'standard,articule'
    fare          REAL    NOT NULL,   -- recette moyenne par passager
    UNIQUE (company_id, code)
);

-- Fréquence cible : départs par heure et par sens, par type de jour et tranche horaire
CREATE TABLE IF NOT EXISTS line_frequency (
    line_id              INTEGER NOT NULL REFERENCES line(id) ON DELETE CASCADE,
    day_type             TEXT    NOT NULL CHECK (day_type IN ('weekday', 'saturday', 'sunday')),
    band                 TEXT    NOT NULL CHECK (band IN ('creuse', 'pointe', 'soir')),
    departures_per_hour  REAL    NOT NULL,
    PRIMARY KEY (line_id, day_type, band)
);

-- Demande de passagers par heure (les deux sens cumulés)
CREATE TABLE IF NOT EXISTS line_demand (
    line_id     INTEGER NOT NULL REFERENCES line(id) ON DELETE CASCADE,
    day_type    TEXT    NOT NULL CHECK (day_type IN ('weekday', 'saturday', 'sunday')),
    hour        INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    passengers  INTEGER NOT NULL,
    PRIMARY KEY (line_id, day_type, hour)
);

-- Un service = une plage horaire sur une ligne, tenue par un bus + un conducteur
CREATE TABLE IF NOT EXISTS service (
    id          INTEGER PRIMARY KEY,
    line_id     INTEGER NOT NULL REFERENCES line(id) ON DELETE CASCADE,
    start_hour  INTEGER NOT NULL,
    end_hour    INTEGER NOT NULL,     -- exclu
    day_type    TEXT    NOT NULL DEFAULT 'all'
                CHECK (day_type IN ('all', 'weekday', 'saturday', 'sunday', 'weekend')),
    vehicle_id  INTEGER REFERENCES vehicle(id) ON DELETE SET NULL,
    driver_id   INTEGER REFERENCES driver(id) ON DELETE SET NULL,
    CHECK (start_hour >= 0 AND end_hour <= 24 AND end_hour > start_hour)
);

CREATE TABLE IF NOT EXISTS ledger (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    day         INTEGER NOT NULL,
    kind        TEXT    NOT NULL,     -- revenue, fuel, maintenance, wages, depot, repair, revision, overhaul, refurbish,
                                      -- ct, purchase, infrastructure, option, capital
    amount      REAL    NOT NULL,     -- positif = recette, négatif = dépense
    label       TEXT    NOT NULL DEFAULT '',
    ref_id      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ledger_company_day ON ledger (company_id, day);

CREATE TABLE IF NOT EXISTS daily_report (
    company_id           INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    day                  INTEGER NOT NULL,
    line_id              INTEGER NOT NULL REFERENCES line(id) ON DELETE CASCADE,
    demand               REAL    NOT NULL,   -- demande effective (après effet réputation)
    served               REAL    NOT NULL,
    revenue              REAL    NOT NULL,   -- après fraude
    fraud_loss           REAL    NOT NULL DEFAULT 0,
    km                   REAL    NOT NULL,
    target_departures    REAL    NOT NULL DEFAULT 0,  -- somme sur la journée des départs/h/sens demandés
    delivered_departures REAL    NOT NULL DEFAULT 0,  -- idem, réellement assurés (plafonnés à la cible)
    PRIMARY KEY (company_id, day, line_id)
);

-- Énergie consommée par jour et par type (L, kg ou kWh), pour estimer les jours de réserve des citernes
CREATE TABLE IF NOT EXISTS energy_use (
    company_id  INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    day         INTEGER NOT NULL,
    energy      TEXT    NOT NULL,
    quantity    REAL    NOT NULL,
    PRIMARY KEY (company_id, day, energy)
);

-- Garantit qu'un jour n'est calculé qu'une seule fois par compagnie
CREATE TABLE IF NOT EXISTS tick_log (
    company_id  INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    day         INTEGER NOT NULL,
    cash_before REAL    NOT NULL,
    cash_after  REAL    NOT NULL,
    PRIMARY KEY (company_id, day)
);

CREATE TABLE IF NOT EXISTS event_log (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    day         INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    vehicle_id  INTEGER,
    message     TEXT    NOT NULL
);
