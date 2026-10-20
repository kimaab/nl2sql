import datetime
from contextlib import contextmanager

import metric_draft
from catalog import read_indexes
from metric_draft import collect, draft_metric
from models import MetricCheck, MetricDraftRequest, MissingMetric, ValueSample
from test_ask import FakeModel as _FakeModel

PG = {"name": "dev", "driver": "postgresql", "db_name": "dev", "db_schema": "", "username": ""}

TABLE = {
    "name": "bms_log",
    "purpose": "차량 BMS 수집 로그",
    "columns": [
        {"name": "vehicle_id", "type": "varchar"},
        {"name": "collect_datetime", "type": "timestamp", "utc_offset": 9},
        {"name": "uman_reel", "type": "numeric", "description": "무인 절단 누적 시간", "needs_calc": True},
    ],
}

# EXPLAIN (FORMAT JSON) 의 한 행: ([{"Plan": ...}],)
INDEX_PLAN = [([{"Plan": {"Node Type": "Limit", "Plans": [{"Node Type": "Index Scan", "Relation Name": "bms_log"}]}}],)]
SEQ_PLAN = [([{"Plan": {"Node Type": "Limit", "Plans": [{"Node Type": "Seq Scan", "Relation Name": "bms_log"}]}}],)]


class FakeModel(_FakeModel):
    """받은 메시지를 그때 모습으로 남긴다 (초안 루프는 메시지 목록에 덧붙여 간다)"""

    def invoke(self, messages):
        return super().invoke(list(messages))


class FakeCursor:
    """answer(sql, params) 가 돌려준 행을 결과로 준다. 실행한 SQL 을 남긴다."""

    def __init__(self, answer):
        self.answer = answer
        self.executed = []
        self.rows = []
        self.description = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.rows = self.answer(sql, params)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def use_cursor(monkeypatch, cur):
    @contextmanager
    def read_only(row, timeout_s=0):
        yield cur

    monkeypatch.setattr(metric_draft, "read_only", read_only)


def test_read_indexes_keeps_usable_leading_columns():
    rows = [
        ("ix_a", "collect_datetime"),
        ("ix_a", None),  # 식 인덱스 칸 — 그 뒤는 쓸 수 없다
        ("ix_a", "vehicle_id"),
        ("ix_b", "vehicle_id"),
        ("ix_c", "nope"),
        ("ix_c", "vehicle_id"),
    ]
    cur = FakeCursor(lambda sql, params: rows)
    out = read_indexes(cur, PG, "bms_log", {"vehicle_id", "collect_datetime"})
    assert out == [{"name": "ix_a", "columns": ["collect_datetime"]}, {"name": "ix_b", "columns": ["vehicle_id"]}]
    assert cur.executed[0][1] == ("public", "bms_log")


def test_read_indexes_mysql_drops_prefix_columns():
    rows = [("PRIMARY", "id", None), ("ix_name", "vehicle_id", 10)]
    cur = FakeCursor(lambda sql, params: rows)
    out = read_indexes(cur, {**PG, "driver": "mysql"}, "bms_log", {"id", "vehicle_id"})
    assert out == [{"name": "PRIMARY", "columns": ["id"]}]


def series_answer(indexes, plan=INDEX_PLAN, reltuples=16_000_000):
    def answer(sql, params):
        if "pg_index" in sql:
            return indexes
        if sql.startswith("EXPLAIN"):
            return plan
        if "reltuples" in sql:
            return [(reltuples,)]
        if sql.startswith('SELECT "vehicle_id" FROM'):
            return [("V1",)]
        if "IS NOT NULL ORDER BY" in sql:
            return [(datetime.datetime(2026, 10, 20, 0, 50), "V1", 178.6)]
        if "ASC" in sql:
            return [(params[1] + datetime.timedelta(minutes=1), "V1", 170.0 + params[1].day)]
        return [(params[2] - datetime.timedelta(minutes=1), "V1", 171.0 + params[1].day)]

    return answer


def test_sample_reads_one_vehicle_per_local_day_through_time_vehicle_index(monkeypatch):
    cur = FakeCursor(series_answer([("bms_log_ts", "collect_datetime"), ("idx_bms_log", "collect_datetime"),
                                    ("idx_bms_log", "vehicle_id")]))
    use_cursor(monkeypatch, cur)
    indexes, sample = collect(PG, TABLE, "uman_reel")
    assert [ix["name"] for ix in indexes] == ["bms_log_ts", "idx_bms_log"]
    assert sample.index == "idx_bms_log"
    assert sample.columns == ["구분", "collect_datetime", "vehicle_id", "uman_reel"]
    # 묶음 후보 1 + 최근 1 + 가장 오래된 날 첫 값 1 + 3일 마지막 값 + 나머지 2일 첫 값
    assert len(sample.queries) == 8
    ran = []
    for sql, params in cur.executed:
        if sql.startswith("SELECT") and "pg_" not in sql:
            ran.append((sql, params))
    # 날짜 경계는 현지(UTC+9) 자정을 UTC 로 옮긴 것
    assert ran[2][1] == ["V1", datetime.datetime(2026, 10, 17, 15), datetime.datetime(2026, 10, 18, 15)]
    for sql, _ in ran:
        assert "ORDER BY" in sql and "LIMIT" in sql
    assert sample.latest == "2026-10-20 09:50:00"
    labels = []
    for r in sample.rows:
        labels.append(r[0])
    assert labels[0] == "2026-10-18 첫 값" and "최근" in labels and "2026-10-20 마지막 값" in labels
    # 실행 전에 매번 실행 계획을 본다
    explains = 0
    for sql, _ in cur.executed:
        if sql.startswith("EXPLAIN"):
            explains += 1
    assert explains == 8


def test_sample_is_skipped_when_plan_scans_big_table(monkeypatch):
    cur = FakeCursor(series_answer([("idx_bms_log", "collect_datetime"), ("idx_bms_log", "vehicle_id")], plan=SEQ_PLAN))
    use_cursor(monkeypatch, cur)
    _, sample = collect(PG, TABLE, "uman_reel")
    assert sample.rows == []
    assert "bms_log 전체를 훑어" in sample.note
    # EXPLAIN 만 하고 본 조회는 실행하지 않았다
    assert not cur.executed[-1][0].startswith('SELECT "vehicle_id" FROM')


def test_small_table_may_be_scanned(monkeypatch):
    cur = FakeCursor(series_answer([("idx_bms_log", "collect_datetime"), ("idx_bms_log", "vehicle_id")],
                                   plan=SEQ_PLAN, reltuples=500))
    use_cursor(monkeypatch, cur)
    _, sample = collect(PG, TABLE, "uman_reel")
    assert sample.rows


def test_sample_is_skipped_without_index(monkeypatch):
    cur = FakeCursor(series_answer([]))
    use_cursor(monkeypatch, cur)
    indexes, sample = collect(PG, TABLE, "uman_reel")
    assert indexes == [] and sample.rows == []
    assert "인덱스가 없어" in sample.note


# ── 초안 루프 ─────────────────────────────────────────────────────────────


def setup_draft(monkeypatch, checks, scans) -> list:
    """checks: 시도마다 등록 확인이 낼 결과, scans: 실행 계획 검사마다 전체를 훑는 테이블.

    가짜 check_metric 도 진짜처럼 변환 · 컴파일 → guard → 실행 순서로 돈다. 돌려준 목록에 실행한 SQL 이 쌓인다.
    """
    contract = {
        "name": "dev",
        "driver": "postgresql",
        "tables": [TABLE],
        "metrics": [
            {
                "name": "무인 가동시간",
                "description": "누적값의 하루 MAX - MIN",
                "tables": ["bms_log"],
                "sql": "SELECT 1",
                "ast": {},
            }
        ],
    }
    executed = []

    def check_metric(sid, sql, guard=None):
        result = checks.pop(0)
        if result.stage in ("parse", "compile"):
            return result
        reason = guard(sql, "SELECT compiled") if guard else None
        if reason:
            return MetricCheck(stage="guard", error=reason)
        executed.append(sql)
        return result

    monkeypatch.setattr(metric_draft, "load_contract", lambda *a, **kw: contract)
    sample = ValueSample(note="샘플 설명", columns=["a"], rows=[["1"]], latest="2026-10-20 09:50:00")
    monkeypatch.setattr(metric_draft, "collect", lambda *a: ([{"name": "idx_bms_log", "columns": ["collect_datetime"]}], sample))
    monkeypatch.setattr(metric_draft, "check_metric", check_metric)
    monkeypatch.setattr(metric_draft, "_plan_full_scans", lambda row, *sqls: scans.pop(0))
    return executed


REQ = MetricDraftRequest(
    question="최근 7일 차량별 무인 절단 시간 합계",
    missing=MissingMetric(name="무인 절단 시간", table="bms_log", column="uman_reel"),
)

OK = MetricCheck(sql="SELECT compiled", tables=["bms_log"])
GOOD_SQL = "SELECT DATE(collect_datetime) AS day, MAX(uman_reel) AS m FROM bms_log GROUP BY DATE(collect_datetime)"


def reply(sql, **more):
    return {"sql": sql, "name": "무인 절단 시간", "synonyms": [], "description": "설명", "assumptions": ["가정"], **more}


def test_positional_refs():
    assert metric_draft.positional_refs("SELECT a, COUNT(*) FROM t GROUP BY 1 ORDER BY 2 DESC", "postgresql") == [
        "GROUP BY",
        "ORDER BY",
    ]
    assert metric_draft.positional_refs("SELECT a FROM (SELECT a FROM t GROUP BY 1) s", "postgresql") == ["GROUP BY"]
    assert metric_draft.positional_refs(GOOD_SQL + " ORDER BY day", "postgresql") == []
    assert metric_draft.positional_refs("SELECT (", "postgresql") == []


def test_draft_is_not_run_until_positional_refs_and_full_scans_are_fixed(monkeypatch):
    executed = setup_draft(monkeypatch, [OK, OK, OK], [["bms_log"], []])
    model = FakeModel(reply(GOOD_SQL.replace("GROUP BY DATE(collect_datetime)", "GROUP BY 1")), reply(GOOD_SQL), reply(GOOD_SQL + ";"))
    draft = draft_metric("sid", REQ, model, PG)
    assert draft.attempts == 3
    # 순번 참조와 전체 훑기는 대상 DB 에서 실행하기 전에 걸렸다 — 실행은 마지막 하나뿐
    assert executed == [GOOD_SQL]
    assert "GROUP BY 에 순번(1, 2 …)을 썼습니다" in model.calls[1][-1].content
    assert "bms_log 전체를 훑어 실행하지 않았습니다" in model.calls[2][-1].content
    assert draft.check.error is None and draft.full_scans == []
    prompt = model.calls[0][1].content
    assert "GROUP BY · ORDER BY 에 순번(1, 2)을 쓰지 않습니다" in model.calls[0][0].content
    assert "[인덱스]\n  - idx_bms_log (collect_datetime)" in prompt
    assert "[같은 테이블 지표]\n- 무인 가동시간" in prompt
    assert "uman_reel (numeric): 무인 절단 누적 시간 [계산 필요]" in prompt
    assert "collect_datetime (timestamp): [UTC +9시간]" in prompt
    assert "[빠진 계산]\n무인 절단 시간 (bms_log.uman_reel)" in prompt
    assert "2026-10-20 까지 3일" in model.calls[0][0].content


def test_draft_retries_on_check_error(monkeypatch):
    executed = setup_draft(monkeypatch, [MetricCheck(stage="parse", error="없는 컬럼입니다"), OK], [[]])
    model = FakeModel(reply("SELECT nope FROM bms_log"), reply(GOOD_SQL))
    draft = draft_metric("sid", REQ, model, PG)
    assert "AST 변환 실패: 없는 컬럼입니다" in model.calls[1][-1].content
    assert executed == [GOOD_SQL] and draft.check.error is None


def test_full_scan_left_after_last_attempt_is_reported_not_run(monkeypatch):
    executed = setup_draft(monkeypatch, [OK, OK, OK], [["bms_log"], ["bms_log"], ["bms_log"]])
    model = FakeModel(reply(GOOD_SQL), reply(GOOD_SQL), reply(GOOD_SQL))
    draft = draft_metric("sid", REQ, model, PG)
    assert executed == []
    assert draft.check.stage == "guard" and draft.full_scans == ["bms_log"]


def test_draft_stops_when_model_is_unsure(monkeypatch):
    setup_draft(monkeypatch, [], [])
    model = FakeModel(reply("", unsure="누적값인지 알 수 없습니다"))
    draft = draft_metric("sid", REQ, model, PG)
    assert draft.unsure == "누적값인지 알 수 없습니다"
    assert draft.check is None and draft.attempts == 1
