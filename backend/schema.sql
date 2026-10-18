-- 메타데이터 DB 스키마 (TO-BE). docs/to-be/메타데이터DB_테이블정의서.xlsx 와 같은 내용이다.
-- 여러 번 실행해도 안전하다 (백엔드 기동 때마다 돈다).

-- 시스템 = 조회 대상 DB. 사용자는 질문할 때 시스템을 먼저 고른다.
CREATE TABLE IF NOT EXISTS meta_system (
    id           UUID PRIMARY KEY,
    code         TEXT        NOT NULL UNIQUE,
    name         TEXT        NOT NULL,
    domain_desc  TEXT        NOT NULL DEFAULT '',  -- 업무 영역 설명. 테이블 추론 프롬프트 첫머리
    driver       TEXT        NOT NULL,
    host         TEXT        NOT NULL,
    port         INTEGER     NOT NULL,
    db_name      TEXT        NOT NULL,
    db_schema    TEXT        NOT NULL DEFAULT '',
    username     TEXT        NOT NULL DEFAULT '',
    password_enc BYTEA       NOT NULL,             -- AES-256-GCM (secret.py). API 로 나가지 않는다
    synced_at    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_system_name ON meta_system (lower(name));

-- 테이블. 동기화는 이름 기준 병합이라 id 가 유지된다 — 지표 연결(metric_table)이 id 를 가리킨다.
CREATE TABLE IF NOT EXISTS meta_table (
    id               UUID PRIMARY KEY,
    system_id        UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    name             TEXT        NOT NULL,
    comment          TEXT        NOT NULL DEFAULT '',   -- 대상 DB 코멘트 (동기화가 채움)
    purpose          TEXT        NOT NULL DEFAULT '',   -- 테이블 용도 (사람)
    deleted_at       TIMESTAMPTZ,                       -- 대상 DB 에서 사라짐 (soft delete)
    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (system_id, name)
);
CREATE INDEX IF NOT EXISTS idx_meta_table_live ON meta_table (system_id) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS meta_column (
    id         BIGSERIAL PRIMARY KEY,
    table_id   UUID        NOT NULL REFERENCES meta_table (id) ON DELETE CASCADE,
    name       TEXT        NOT NULL,
    data_type  TEXT        NOT NULL DEFAULT '',
    comment    TEXT        NOT NULL DEFAULT '',
    ordinal    INTEGER     NOT NULL,
    is_pk      BOOLEAN     NOT NULL DEFAULT false,
    deleted_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (table_id, name)
);
CREATE INDEX IF NOT EXISTS idx_meta_column_table ON meta_column (table_id);
-- UTC 로 저장된 시각 컬럼의 현지 시차(시간, 예: KST = 9). 사람이 적고, 동기화는 건드리지 않는다.
-- 컴파일러가 날짜 묶음·표시는 현지 시각으로, 필터 값은 UTC 로 바꿔 비교한다.
ALTER TABLE meta_column ADD COLUMN IF NOT EXISTS utc_offset_hours SMALLINT;
-- 계산이 필요한 컬럼 (예: 누적값 — 그대로 합치면 틀린다). 사람이 표시하고, 동기화는 건드리지 않는다.
-- 이 컬럼을 계산해야 답할 수 있는 질문은 지표가 필요하다 — 테이블 선택 단계의 LLM 이 판단한다.
ALTER TABLE meta_column ADD COLUMN IF NOT EXISTS needs_calc BOOLEAN NOT NULL DEFAULT false;

-- 테이블 관계. FK 는 동기화가(source='fk'), 나머지는 사람이(source='manual') 관리한다.
CREATE TABLE IF NOT EXISTS meta_relation (
    id              UUID PRIMARY KEY,
    system_id       UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    left_table_id   UUID        NOT NULL REFERENCES meta_table (id) ON DELETE CASCADE,
    left_column     TEXT        NOT NULL,
    right_table_id  UUID        NOT NULL REFERENCES meta_table (id) ON DELETE CASCADE,
    right_column    TEXT        NOT NULL,
    constraint_name TEXT        NOT NULL DEFAULT '',
    source          TEXT        NOT NULL DEFAULT 'manual',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_meta_relation_system ON meta_relation (system_id);

-- 지표 = 이름 · 설명 · 같은 말 + SQL. SQL 을 조회 명세(AST)로 옮겨 저장하고, 질문마다 LLM 이 조건만 바꿔 쓴다.
CREATE TABLE IF NOT EXISTS metric (
    id            UUID PRIMARY KEY,
    system_id     UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    name          TEXT        NOT NULL,
    description   TEXT        NOT NULL DEFAULT '',
    synonyms      JSONB       NOT NULL DEFAULT '[]',
    source_sql    TEXT        NOT NULL,                 -- 등록한 SQL 원문
    ast           JSONB       NOT NULL,                 -- SQL 을 옮긴 조회 명세 (compiler.py)
    status        TEXT        NOT NULL DEFAULT 'active', -- active | broken (동기화 뒤 AST 가 컴파일되지 않음)
    broken_reason TEXT,
    version       INTEGER     NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (system_id, name)
);
-- 예전 형식(테이블 · 디멘젼 · 계산식) 지표는 옮기지 않고 지운다 — SQL 로 다시 등록한다. 정의는 metric_history 에 남아 있다.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'metric' AND column_name = 'base_table_id') THEN
        DELETE FROM metric;
        ALTER TABLE metric
            DROP COLUMN kind, DROP COLUMN base_table_id, DROP COLUMN joins, DROP COLUMN agg_field,
            DROP COLUMN agg_function, DROP COLUMN select_columns, DROP COLUMN expression, DROP COLUMN series,
            DROP COLUMN IF EXISTS measures, DROP COLUMN fixed_filters, DROP COLUMN IF EXISTS dimensions,
            ADD COLUMN source_sql TEXT NOT NULL, ADD COLUMN ast JSONB NOT NULL;
        ALTER TABLE metric ALTER COLUMN status SET DEFAULT 'active';
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_metric_active ON metric (system_id) WHERE status = 'active';

-- 지표가 쓰는 테이블. 테이블 선택 단계에서 테이블마다 지표 설명을 붙일 때 쓴다.
CREATE TABLE IF NOT EXISTS metric_table (
    metric_id UUID NOT NULL REFERENCES metric (id) ON DELETE CASCADE,
    table_id  UUID NOT NULL REFERENCES meta_table (id) ON DELETE CASCADE,
    role      TEXT NOT NULL DEFAULT 'base',  -- base | join
    PRIMARY KEY (metric_id, table_id)
);
CREATE INDEX IF NOT EXISTS idx_metric_table_table ON metric_table (table_id);

-- 지표를 바꾸거나 지울 때마다 그 시점의 정의를 남긴다. 지표가 지워져도 남는다.
CREATE TABLE IF NOT EXISTS metric_history (
    id         BIGSERIAL PRIMARY KEY,
    system_id  UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    metric_id  UUID        NOT NULL,
    version    INTEGER     NOT NULL,
    action     TEXT        NOT NULL,  -- create | update | delete
    snapshot   JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metric_history_metric ON metric_history (metric_id, version);

CREATE TABLE IF NOT EXISTS meta_sync_log (
    id             BIGSERIAL PRIMARY KEY,
    system_id      UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    trigger        TEXT        NOT NULL,  -- manual | auto
    status         TEXT        NOT NULL,  -- ok | error
    tables_added   INTEGER,
    tables_changed INTEGER,
    tables_removed INTEGER,
    broken_metrics JSONB       NOT NULL DEFAULT '[]',
    error          TEXT,
    started_at     TIMESTAMPTZ NOT NULL,
    finished_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_meta_sync_log_system ON meta_sync_log (system_id, started_at DESC);

-- 질문 기록. 다음 질문의 프롬프트에는 실리지 않는다 — 사람이 다시 보고 평가하기 위한 기록이다.
CREATE TABLE IF NOT EXISTS ask_log (
    id               UUID PRIMARY KEY,
    system_id        UUID        NOT NULL REFERENCES meta_system (id) ON DELETE CASCADE,
    question         TEXT        NOT NULL,
    selected_tables  JSONB       NOT NULL DEFAULT '[]',
    selected_metrics JSONB       NOT NULL DEFAULT '[]',
    ast              JSONB,
    sql              TEXT,
    error            TEXT,
    clarification    TEXT,
    attempts         INTEGER     NOT NULL DEFAULT 0,
    total_tokens     INTEGER     NOT NULL DEFAULT 0,
    elapsed_ms       INTEGER     NOT NULL DEFAULT 0,
    favorite         BOOLEAN     NOT NULL DEFAULT false,
    feedback         TEXT,        -- up | down
    feedback_note    TEXT        NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ask_log_system ON ask_log (system_id, created_at DESC);

-- 질문마다 각 단계에서 LLM 에 보낸 후보, 고른 것, 이유, 토큰
CREATE TABLE IF NOT EXISTS ask_trace (
    id                BIGSERIAL PRIMARY KEY,
    ask_id            UUID        NOT NULL REFERENCES ask_log (id) ON DELETE CASCADE,
    stage             TEXT        NOT NULL,  -- table | metric | sql
    candidates        JSONB       NOT NULL DEFAULT '[]',
    selected          JSONB       NOT NULL DEFAULT '[]',
    reason            TEXT        NOT NULL DEFAULT '',
    prompt_tokens     INTEGER     NOT NULL DEFAULT 0,
    completion_tokens INTEGER     NOT NULL DEFAULT 0,
    elapsed_ms        INTEGER     NOT NULL DEFAULT 0,
    model             TEXT        NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ask_trace_ask ON ask_trace (ask_id, stage);

-- 정리: 쓰지 않게 된 표 · 컬럼을 지운다 (이미 지웠으면 아무 일도 하지 않는다)
DROP TABLE IF EXISTS eval_result, eval_run, eval_case, enrich_job, glossary, metric_example,
                     metric_formula, formula, ask_log_v1;
ALTER TABLE meta_table
    DROP COLUMN IF EXISTS card, DROP COLUMN IF EXISTS card_line,
    DROP COLUMN IF EXISTS card_status, DROP COLUMN IF EXISTS card_source_hash;
ALTER TABLE meta_column DROP COLUMN IF EXISTS synonyms, DROP COLUMN IF EXISTS codes;
ALTER TABLE metric_table DROP COLUMN IF EXISTS origin;
ALTER TABLE metric_history DROP COLUMN IF EXISTS actor;
