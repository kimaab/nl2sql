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
ALTER TABLE datasource_metric ADD COLUMN IF NOT EXISTS joins JSONB NOT NULL DEFAULT '[]';
-- kind='derived'일 때만 채워진다. "[매출] - [환불]" 처럼 집계형 지표를 잇는 수식.
ALTER TABLE datasource_metric ADD COLUMN IF NOT EXISTS expression TEXT;
-- [{"question": "...", "ast": {...}}] — 프롬프트 예시와 검색 본문으로 쓰인다.
ALTER TABLE datasource_metric ADD COLUMN IF NOT EXISTS examples JSONB NOT NULL DEFAULT '[]';
ALTER TABLE datasource_metric ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE datasource_metric ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- 지표를 바꾸거나 지울 때마다 그 시점의 정의를 남긴다. 지표가 지워져도 이력은 남는다.
CREATE TABLE IF NOT EXISTS datasource_metric_history (
    id            BIGSERIAL PRIMARY KEY,
    datasource_id UUID        NOT NULL REFERENCES datasource (id) ON DELETE CASCADE,
    metric_id     UUID        NOT NULL,
    version       INTEGER     NOT NULL,
    action        TEXT        NOT NULL,  -- create | update | delete
    snapshot      JSONB       NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 테이블 사이의 관계. 동기화가 대상 DB의 FK를 source='fk'로 통째 교체하고,
-- 사람이 등록한 source='manual'은 동기화가 건드리지 않는다.
-- 테이블·컬럼은 이름으로 들고 있다 — 동기화가 datasource_table을 지우고 다시 넣기 때문이다.
CREATE TABLE IF NOT EXISTS datasource_relation (
    id              UUID PRIMARY KEY,
    datasource_id   UUID NOT NULL REFERENCES datasource (id) ON DELETE CASCADE,
    constraint_name TEXT NOT NULL DEFAULT '',
    left_table      TEXT NOT NULL,
    left_column     TEXT NOT NULL,
    right_table     TEXT NOT NULL,
    right_column    TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'manual',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 컬럼 동의어와 코드값 사전. 같은 이유로 이름으로 들고 있어 동기화 뒤에도 남는다.
CREATE TABLE IF NOT EXISTS column_annotation (
    datasource_id UUID  NOT NULL REFERENCES datasource (id) ON DELETE CASCADE,
    table_name    TEXT  NOT NULL,
    column_name   TEXT  NOT NULL,
    synonyms      JSONB NOT NULL DEFAULT '[]',
    codes         JSONB NOT NULL DEFAULT '[]',  -- [{"code": "03", "label": "배송완료"}]
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (datasource_id, table_name, column_name)
);

-- 동기화 실행 기록. 깨진 지표가 생기면 broken_metrics 에 남는다.
CREATE TABLE IF NOT EXISTS datasource_sync_log (
    id             BIGSERIAL PRIMARY KEY,
    datasource_id  UUID        NOT NULL REFERENCES datasource (id) ON DELETE CASCADE,
    trigger        TEXT        NOT NULL,  -- manual | auto
    status         TEXT        NOT NULL,  -- ok | error
    table_count    INTEGER,
    column_count   INTEGER,
    relation_count INTEGER,
    broken_metrics JSONB       NOT NULL DEFAULT '[]',
    error          TEXT,
    started_at     TIMESTAMPTZ NOT NULL,
    finished_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 질문 기록. 서버의 대화 기억이 아니라 사람이 다시 보고 평가하기 위한 기록이다 —
-- 다음 질문의 프롬프트에는 실리지 않는다.
CREATE TABLE IF NOT EXISTS ask_log (
    id            UUID PRIMARY KEY,
    datasource_id UUID        NOT NULL REFERENCES datasource (id) ON DELETE CASCADE,
    question      TEXT        NOT NULL,
    sql           TEXT,
    ast           JSONB,
    error         TEXT,
    clarification TEXT,
    attempts      INTEGER     NOT NULL DEFAULT 0,
    elapsed_ms    INTEGER     NOT NULL DEFAULT 0,
    favorite      BOOLEAN     NOT NULL DEFAULT false,
    feedback      TEXT,       -- up | down
    feedback_note TEXT        NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_datasource_table_ds ON datasource_table (datasource_id);
CREATE INDEX IF NOT EXISTS idx_datasource_column_table ON datasource_column (table_id);
CREATE INDEX IF NOT EXISTS idx_datasource_metric_ds ON datasource_metric (datasource_id);
CREATE INDEX IF NOT EXISTS idx_metric_history_metric ON datasource_metric_history (metric_id, version);
CREATE INDEX IF NOT EXISTS idx_relation_ds ON datasource_relation (datasource_id);
CREATE INDEX IF NOT EXISTS idx_sync_log_ds ON datasource_sync_log (datasource_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ask_log_ds ON ask_log (datasource_id, created_at DESC);
