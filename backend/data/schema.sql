-- Business Saathi ledger. Eleven tables; each is used by a named capability.
-- See docs/DESIGN.md "SQLite schema" for why each one earns its place.

PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS merchants;
DROP TABLE IF EXISTS txn_hourly;
DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS merchant_patterns;
DROP TABLE IF EXISTS situations;
DROP TABLE IF EXISTS actions;
DROP TABLE IF EXISTS outcomes;
DROP TABLE IF EXISTS learned_patterns;
DROP TABLE IF EXISTS obligations;
DROP TABLE IF EXISTS stock_snapshots;
DROP TABLE IF EXISTS merchant_inputs;
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS alerts;
DROP TABLE IF EXISTS meta;

-- ============================ CORE (tier A) ============================

CREATE TABLE merchants (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    locality        TEXT NOT NULL,
    locality_type   TEXT NOT NULL,
    opened_on       TEXT NOT NULL,
    volume_band     TEXT NOT NULL,
    avg_daily       REAL NOT NULL,      -- generator target; analytics never reads this
    hourly_vector   TEXT NOT NULL,      -- JSON, 16 floats for hours 7..22, normalised
    has_obligations INTEGER NOT NULL DEFAULT 0,
    has_stock_feed  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_merchants_cohort ON merchants(category, locality_type, volume_band);

-- Hourly buckets, not individual payments: every analysis here is hour-band
-- based, so the bucket loses nothing and keeps the DB at ~900k rows.
CREATE TABLE txn_hourly (
    merchant_id TEXT NOT NULL,
    day         TEXT NOT NULL,
    hour        INTEGER NOT NULL,
    txns        INTEGER NOT NULL,
    amount      REAL NOT NULL,
    PRIMARY KEY (merchant_id, day, hour)
) WITHOUT ROWID;
CREATE INDEX idx_txn_day ON txn_hourly(day);
CREATE INDEX idx_txn_merchant_day ON txn_hourly(merchant_id, day);

-- Individual payments for the demo cohort's last 45 days only. Proof that the
-- bucket above is an aggregation choice, not a limitation.
CREATE TABLE payments (
    id          INTEGER PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    ts          TEXT NOT NULL,
    amount      REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'success'   -- success|failed|refunded
);
CREATE INDEX idx_payments_merchant_ts ON payments(merchant_id, ts);

-- Cached derived baselines, recomputed by scripts/recompute_patterns.py.
CREATE TABLE merchant_patterns (
    merchant_id  TEXT NOT NULL,
    pattern_type TEXT NOT NULL,   -- hourly|weekday|baseline_30d|avg_ticket|volatility
    computed_on  TEXT NOT NULL,
    payload      TEXT NOT NULL,   -- JSON
    PRIMARY KEY (merchant_id, pattern_type)
) WITHOUT ROWID;

-- A detected business condition: the unit the graph reasons about, and the
-- join key between "what happened" and "what someone did about it".
CREATE TABLE situations (
    id            INTEGER PRIMARY KEY,
    merchant_id   TEXT NOT NULL,
    kind          TEXT NOT NULL,   -- evening_decline|sales_decline|demand_surge|volatility|flat
    detected_on   TEXT NOT NULL,
    severity      REAL NOT NULL,   -- signed % deviation from own baseline
    band          TEXT,
    peer_relative TEXT,            -- specific_to_merchant|market_wide|outperforming|unknown
    payload       TEXT NOT NULL
);
CREATE INDEX idx_situations_merchant ON situations(merchant_id, detected_on);
CREATE INDEX idx_situations_kind ON situations(kind);

CREATE TABLE actions (
    id           INTEGER PRIMARY KEY,
    merchant_id  TEXT NOT NULL,
    situation_id INTEGER,
    type         TEXT NOT NULL,
    params       TEXT NOT NULL,   -- JSON
    started_on   TEXT NOT NULL,
    ended_on     TEXT NOT NULL,
    source       TEXT NOT NULL,   -- historical|live
    run_id       TEXT
);
CREATE INDEX idx_actions_merchant ON actions(merchant_id);
CREATE INDEX idx_actions_type ON actions(type);

CREATE TABLE outcomes (
    id          INTEGER PRIMARY KEY,
    action_id   INTEGER NOT NULL,
    metric      TEXT NOT NULL,
    before      REAL NOT NULL,
    after       REAL NOT NULL,
    delta_pct   REAL NOT NULL,
    verdict     TEXT NOT NULL,   -- recovered|sustained|no_change|temporary_spike|worse
    measured_on TEXT NOT NULL,
    simulated   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_outcomes_action ON outcomes(action_id);

-- The aggregate the graph serves and the write-back updates. Denormalised on
-- purpose: this row is what visibly changes on screen when the system learns.
CREATE TABLE learned_patterns (
    id             INTEGER PRIMARY KEY,
    cohort_key     TEXT NOT NULL,
    situation_kind TEXT NOT NULL,
    action_type    TEXT NOT NULL,
    tried          INTEGER NOT NULL,
    worked         INTEGER NOT NULL,
    median_delta   REAL,
    updated_on     TEXT NOT NULL,
    UNIQUE (cohort_key, situation_kind, action_type)
);

-- ========================== OPTIONAL (tier B) ==========================

CREATE TABLE obligations (
    id          INTEGER PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    due_on      TEXT NOT NULL,
    amount      REAL NOT NULL,
    label       TEXT NOT NULL,   -- rent|supplier|salary|loan_emi
    recurring   INTEGER NOT NULL,
    source      TEXT NOT NULL    -- integration|merchant_stated
);
CREATE INDEX idx_obligations_merchant ON obligations(merchant_id, due_on);

CREATE TABLE stock_snapshots (
    id          INTEGER PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    item_label  TEXT NOT NULL,   -- free text; never assumed to be a SKU
    quantity    REAL NOT NULL,
    unit        TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    source      TEXT NOT NULL
);
CREATE INDEX idx_stock_merchant ON stock_snapshots(merchant_id, as_of);

-- ======================= CONVERSATIONAL (tier C) =======================

CREATE TABLE merchant_inputs (
    id          INTEGER PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    kind        TEXT NOT NULL,
    subject     TEXT,
    value_num   REAL,
    value_text  TEXT,
    unit        TEXT,
    stated_at   TEXT NOT NULL,
    expires_at  TEXT NOT NULL,   -- enforced in the query, not by a cleanup job
    utterance   TEXT NOT NULL,   -- the merchant's own words, shown on /ops
    confidence  REAL NOT NULL DEFAULT 0.8
);
CREATE INDEX idx_inputs_live ON merchant_inputs(merchant_id, kind, expires_at);

-- =========================== INFRASTRUCTURE ============================

CREATE TABLE events (
    id         INTEGER PRIMARY KEY,
    query_id   TEXT,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_events_query ON events(query_id);

CREATE TABLE alerts (
    id          INTEGER PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    alert_type  TEXT NOT NULL,
    severity    REAL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_alerts_merchant ON alerts(merchant_id, created_at);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
