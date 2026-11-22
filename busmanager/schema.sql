-- Schéma du jeu (niveau 1). SQLite.
-- Invariant : company.cash = capital initial + somme des écritures du ledger.

CREATE TABLE IF NOT EXISTS company (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    cash        REAL    NOT NULL DEFAULT 0,
    game_day    INTEGER NOT NULL DEFAULT 0,   -- prochain jour à simuler
    reputation  REAL    NOT NULL DEFAULT 50,  -- 0..100
    level       INTEGER NOT NULL DEFAULT 1,
    seed        INTEGER NOT NULL DEFAULT 1    -- graine du hasard (pannes), pour la reproductibilité
);

-- Catalogue de modèles (données de référence, partagées par toutes les compagnies)
CREATE TABLE IF NOT EXISTS bus_model (
    id                      INTEGER PRIMARY KEY,
    name                    TEXT    NOT NULL UNIQUE,
    energy                  TEXT    NOT NULL CHECK (energy IN ('diesel', 'gnv', 'electrique')),
    body                    TEXT    NOT NULL CHECK (body IN ('standard', 'articule')),
    capacity                INTEGER NOT NULL,
    consumption_per_100km   REAL    NOT NULL,  -- L (diesel), kg (gnv) ou kWh (électrique)
    price                   REAL    NOT NULL,
    reliability             REAL    NOT NULL,  -- 0..1, plus haut = moins de pannes
    maintenance_cost_per_km REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS depot (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES company(id),
    name        TEXT    NOT NULL,
    capacity    INTEGER NOT NULL               -- nombre de places de bus
);

CREATE TABLE IF NOT EXISTS vehicle (
    id                INTEGER PRIMARY KEY,
    company_id        INTEGER NOT NULL REFERENCES company(id),
    model_id          INTEGER NOT NULL REFERENCES bus_model(id),
    depot_id          INTEGER NOT NULL REFERENCES depot(id),
    plate             TEXT    NOT NULL UNIQUE,
    km                REAL    NOT NULL DEFAULT 0,
    engine_state      REAL    NOT NULL DEFAULT 100,  -- 0..100
    interior_state    REAL    NOT NULL DEFAULT 100,  -- 0..100
    revision_due_day  INTEGER NOT NULL,
    ct_due_day        INTEGER NOT NULL,              -- contrôle technique
    status            TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'workshop')),
    available_from_day INTEGER NOT NULL DEFAULT 0,   -- si 'workshop' : jour de retour en service
    options           TEXT    NOT NULL DEFAULT '[]'  -- JSON, réservé (stock d'options : niveau 2)
);

CREATE TABLE IF NOT EXISTS driver (
    id                  INTEGER PRIMARY KEY,
    company_id          INTEGER NOT NULL REFERENCES company(id),
    name                TEXT    NOT NULL,
    daily_wage          REAL    NOT NULL,
    articulated_licence INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS line (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES company(id),
    code          TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    distance_km   REAL    NOT NULL,   -- distance d'un trajet (sens unique)
    duration_min  REAL    NOT NULL,   -- durée d'un trajet (sens unique)
    allowed_body  TEXT    NOT NULL DEFAULT 'any' CHECK (allowed_body IN ('standard', 'any')),
    fare          REAL    NOT NULL,   -- prix moyen payé par passager
    UNIQUE (company_id, code)
);

-- Demande de passagers par heure (les deux sens cumulés)
CREATE TABLE IF NOT EXISTS line_demand (
    line_id     INTEGER NOT NULL REFERENCES line(id),
    day_type    TEXT    NOT NULL CHECK (day_type IN ('weekday', 'weekend')),
    hour        INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    passengers  INTEGER NOT NULL,
    PRIMARY KEY (line_id, day_type, hour)
);

-- Un service = une plage horaire sur une ligne, tenue par un bus + un conducteur
CREATE TABLE IF NOT EXISTS service (
    id          INTEGER PRIMARY KEY,
    line_id     INTEGER NOT NULL REFERENCES line(id),
    start_hour  INTEGER NOT NULL,
    end_hour    INTEGER NOT NULL,     -- exclu
    vehicle_id  INTEGER REFERENCES vehicle(id),
    driver_id   INTEGER REFERENCES driver(id),
    CHECK (start_hour >= 0 AND end_hour <= 24 AND end_hour > start_hour)
);

CREATE TABLE IF NOT EXISTS ledger (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES company(id),
    day         INTEGER NOT NULL,
    kind        TEXT    NOT NULL,     -- revenue, fuel, maintenance, wages, depot, repair, purchase, revision, ct, capital
    amount      REAL    NOT NULL,     -- positif = recette, négatif = dépense
    label       TEXT    NOT NULL DEFAULT '',
    ref_id      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ledger_company_day ON ledger (company_id, day);

CREATE TABLE IF NOT EXISTS daily_report (
    company_id  INTEGER NOT NULL REFERENCES company(id),
    day         INTEGER NOT NULL,
    line_id     INTEGER NOT NULL REFERENCES line(id),
    demand      REAL    NOT NULL,     -- demande effective (après effet réputation)
    served      REAL    NOT NULL,
    revenue     REAL    NOT NULL,
    km          REAL    NOT NULL,
    PRIMARY KEY (company_id, day, line_id)
);

-- Garantit qu'un jour n'est calculé qu'une seule fois par compagnie
CREATE TABLE IF NOT EXISTS tick_log (
    company_id  INTEGER NOT NULL REFERENCES company(id),
    day         INTEGER NOT NULL,
    cash_before REAL    NOT NULL,
    cash_after  REAL    NOT NULL,
    PRIMARY KEY (company_id, day)
);

CREATE TABLE IF NOT EXISTS event_log (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES company(id),
    day         INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    vehicle_id  INTEGER,
    message     TEXT    NOT NULL
);
