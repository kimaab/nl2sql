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
    -- 'aggregate' = 집계 하나를 내는 지표, 'projection' = 컬럼 몇 개를 조회하는 지표.
    -- 어느 쪽이든 fixed_filters는 컴파일러가 강제로 붙인다.
    kind          TEXT NOT NULL DEFAULT 'aggregate',
    -- kind='aggregate'일 때만 채워진다.
    agg_field     TEXT,
    agg_function  TEXT,
    -- kind='projection'일 때만 채워진다. ["컬럼1","컬럼2", ...]
    select_columns JSONB NOT NULL DEFAULT '[]',
    fixed_filters JSONB NOT NULL DEFAULT '[]',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (datasource_id, name)
);

-- 이미 만들어진 표를 위한 이행. CREATE TABLE IF NOT EXISTS는 기존 표를 건드리지
-- 않으므로, 컬럼 추가는 여기서 따로 한다. 전부 여러 번 실행해도 안전하다.
ALTER TABLE datasource_metric ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'aggregate';
ALTER TABLE datasource_metric ADD COLUMN IF NOT EXISTS select_columns JSONB NOT NULL DEFAULT '[]';
ALTER TABLE datasource_metric ALTER COLUMN agg_field DROP NOT NULL;
ALTER TABLE datasource_metric ALTER COLUMN agg_function DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_datasource_table_ds ON datasource_table (datasource_id);
CREATE INDEX IF NOT EXISTS idx_datasource_column_table ON datasource_column (table_id);
CREATE INDEX IF NOT EXISTS idx_datasource_metric_ds ON datasource_metric (datasource_id);
