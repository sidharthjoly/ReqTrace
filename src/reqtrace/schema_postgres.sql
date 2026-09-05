-- Postgres schema. Applied automatically when DATABASE_URL is set.
-- Search is tsvector + pg_trgm; no Meilisearch/Typesense until Postgres
-- actually stops being enough.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS companies (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    domain       TEXT,
    ats_vendor   TEXT NOT NULL,
    board_token  TEXT NOT NULL,
    careers_url  TEXT,
    UNIQUE (ats_vendor, board_token)
);

CREATE TABLE IF NOT EXISTS jobs (
    ats_vendor        TEXT NOT NULL,
    board_token       TEXT NOT NULL,
    external_id       TEXT NOT NULL,
    title             TEXT NOT NULL,
    description_html  TEXT,
    description_text  TEXT,
    location_raw      TEXT,
    location_city     TEXT,
    location_country  TEXT,
    remote_type       TEXT,
    salary_min        DOUBLE PRECISION,
    salary_max        DOUBLE PRECISION,
    salary_currency   TEXT,
    salary_period     TEXT,
    department        TEXT,
    employment_type   TEXT,
    seniority         TEXT,
    function          TEXT,
    apply_url         TEXT,
    posted_at         TIMESTAMPTZ,
    content_hash      TEXT,
    first_seen_at     TIMESTAMPTZ NOT NULL,
    last_seen_at      TIMESTAMPTZ NOT NULL,
    closed_at         TIMESTAMPTZ,
    -- Job identity is the ATS's own id, scoped to the board it came from.
    -- Never title+company: that merges genuinely distinct requisitions.
    PRIMARY KEY (ats_vendor, board_token, external_id)
);

CREATE TABLE IF NOT EXISTS board_runs (
    id           BIGSERIAL PRIMARY KEY,
    ats_vendor   TEXT NOT NULL,
    board_token  TEXT NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL,
    complete     BOOLEAN NOT NULL,
    n_fetched    INTEGER NOT NULL,
    n_new        INTEGER NOT NULL,
    n_updated    INTEGER NOT NULL,
    n_closed     INTEGER NOT NULL,
    n_reopened   INTEGER NOT NULL,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS jobs_open_idx  ON jobs (ats_vendor, board_token, closed_at);
CREATE INDEX IF NOT EXISTS jobs_city_idx  ON jobs (location_city, closed_at);
CREATE INDEX IF NOT EXISTS jobs_seen_idx  ON jobs (first_seen_at DESC);
CREATE INDEX IF NOT EXISTS jobs_posted_idx ON jobs (posted_at DESC);

-- Full-text over title + body, weighted so a title hit outranks a body mention.
CREATE INDEX IF NOT EXISTS jobs_fts_idx ON jobs USING GIN (
    (setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
     setweight(to_tsvector('english', coalesce(description_text, '')), 'B'))
);

-- Trigram index for fuzzy title matching ("data scientst").
CREATE INDEX IF NOT EXISTS jobs_title_trgm_idx ON jobs USING GIN (title gin_trgm_ops);
