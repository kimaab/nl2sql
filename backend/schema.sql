CREATE TABLE IF NOT EXISTS datasource (
    id          UUID PRIMARY KEY,
    name        TEXT        NOT NULL UNIQUE,
    description TEXT        NOT NULL DEFAULT '',
    driver      TEXT        NOT NULL,
    host        TEXT        NOT NULL,
    port        INTEGER     NOT NULL,
    db_name     TEXT        NOT NULL,
    db_schema   TEXT        NOT NULL DEFAULT '',
    username    TEXT        NOT NULL DEFAULT '',
    password    TEXT        NOT NULL DEFAULT '',
    synced_at   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS datasource_table (
    id            UUID PRIMARY KEY,
    datasource_id UUID NOT NULL REFERENCES datasource (id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    UNIQUE (datasource_id, name)
);

CREATE TABLE IF NOT EXISTS datasource_column (
    id          BIGSERIAL PRIMARY KEY,
    table_id    UUID    NOT NULL REFERENCES datasource_table (id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    data_type   TEXT    NOT NULL DEFAULT '',
    description TEXT    NOT NULL DEFAULT '',
    ordinal     INTEGER NOT NULL,
    UNIQUE (table_id, name)
);

CREATE TABLE IF NOT EXISTS datasource_metric (
    id            UUID PRIMARY KEY,
    datasource_id UUID NOT NULL REFERENCES datasource (id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    table_name    TEXT NOT NULL,
    agg_field     TEXT NOT NULL,
    agg_function  TEXT NOT NULL,
    fixed_filters JSONB NOT NULL DEFAULT '[]',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (datasource_id, name)
);

CREATE INDEX IF NOT EXISTS idx_datasource_table_ds ON datasource_table (datasource_id);
CREATE INDEX IF NOT EXISTS idx_datasource_column_table ON datasource_column (table_id);
CREATE INDEX IF NOT EXISTS idx_datasource_metric_ds ON datasource_metric (datasource_id);
