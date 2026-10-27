import datetime
from contextlib import contextmanager

import metric_auto
import metric_validate
from compiler import Compiler
from metric_auto import _candidate, pick_tables
from metric_validate import overall, validate_metric
from models import AutoRunRequest, MetricCheck
from selector import Step
from sql_ast import parse_sql
from test_ask import FakeModel

PG = {"driver": "postgresql", "name": "dev"}

CONTRACT = {
    "name": "dev",
    "driver": "postgresql",
    "tables": [
        {"name": "sales", "description": "", "purpose": "", "columns": [
            {"name": "sold_date", "type": "date", "description": ""},
            {"name": "store_id", "type": "integer", "description": ""},
            {"name": "amount", "type": "numeric(7,2)", "description": ""},
        ]},
        {"name": "store", "description": "", "purpose": "", "columns": [
            {"name": "store_id", "type": "integer", "description": ""},
            {"name": "name", "type": "text", "description": ""},
        ]},
        {"name": "returns", "description": "", "purpose": "", "columns": [
            {"name": "store_id", "type": "integer", "description": ""},
        ]},
    ],
    "relations": [
        {"left_table": "sales", "left_column": "store_id", "right_table": "store", "right_column": "store_id"},
        {"left_table": "returns", "left_column": "store_id", "right_table": "store", "right_column": "store_id"},
        {"left_table": "sales", "left_column": "sold_date", "right_table": "store", "right_column": "store_id"},
    ],
    "metrics": [],
}

SQL = "SELECT sold_date, SUM(amount) AS total FROM sales GROUP BY sold_date"


def proposal(sql=SQL, name="일별 매출"):
    return {"name": name, "description": "판매 금액의 날짜별 합계", "synonyms": ["매출"], "sql": sql, "rationale": "기본 매출"}


def setup(monkeypatch, checks, scans=None):
    """checks: 시도마다 등록 확인 결과. 가짜 check_metric 도 guard 를 부른다. 저장한 지표 이름이 돌려준 목록에 쌓인다."""
    saved = []

    def check_metric(sid, sql, guard=None):
        result = checks.pop(0)
        reason = guard(sql, "SELECT compiled") if guard else None
        if reason:
            return MetricCheck(sql="SELECT compiled", stage="guard", error=reason)
        return result

    class Saved:
        def __init__(self, name):
            self.id = "00000000-0000-0000-0000-000000000001"
            self.name = name

    def create_metric(sid, data):
        saved.append(data.name)
        return Saved(data.name)

    monkeypatch.setattr(metric_auto, "check_metric", check_metric)
    monkeypatch.setattr(metric_auto, "create_metric", create_metric)
    monkeypatch.setattr(metric_auto, "plan_full_scans", lambda row, *sqls: list(scans or []))
    return saved


def run(model, p, taken=None, allow_full_scan=False):
    return _candidate("sid", PG, model, [], p, Step("t"), taken if taken is not None else set(), "sales", allow_full_scan)


def test_candidate_is_fixed_with_check_error_then_registered(monkeypatch):
    saved = setup(monkeypatch, [MetricCheck(stage="verify", error="결과가 다릅니다"), MetricCheck(sql="SELECT ok", rows=3)])
    model = FakeModel(proposal(SQL + " ORDER BY sold_date"))
    item = run(model, proposal())
    assert item["status"] == "registered"
    assert item["attempts"] == 2
    assert [t["stage"] for t in item["tries"]] == ["verify", None]
    assert saved == ["일별 매출"]
    # 고쳐 달라는 요청에 단계와 오류가 실린다
    assert "결과 비교 실패: 결과가 다릅니다" in model.calls[0][-1].content


def test_duplicate_name_is_skipped_without_running(monkeypatch):
    saved = setup(monkeypatch, [])
    item = run(FakeModel(), proposal(), taken={"일별 매출"})
    assert item["status"] == "skipped"
    assert item["stage"] == "duplicate"
    assert saved == []


def test_full_scan_blocks_unless_allowed(monkeypatch):
    setup(monkeypatch, [MetricCheck()] * 3, scans=["sales"])
    model = FakeModel(proposal(), proposal())
    item = run(model, proposal())
    assert item["status"] == "failed"
    assert item["stage"] == "guard"
    assert item["attempts"] == 3
    assert "sales 전체를 훑어" in item["reason"]

    saved = setup(monkeypatch, [MetricCheck(sql="SELECT ok")], scans=["sales"])
    item = run(FakeModel(), proposal(), allow_full_scan=True)
    assert item["status"] == "registered"
    assert item["full_scans"] == ["sales"]
    assert saved == ["일별 매출"]


def test_unsure_fix_stops(monkeypatch):
    setup(monkeypatch, [MetricCheck(stage="parse", error="UNION 은 지원하지 않습니다")])
    model = FakeModel({"sql": "", "unsure": "채널을 합치려면 UNION 이 필요합니다"})
    item = run(model, proposal())
    assert item["status"] == "failed"
    assert item["stage"] == "llm"
    assert item["reason"] == "채널을 합치려면 UNION 이 필요합니다"


def test_pick_tables_prefers_tables_without_metrics_and_many_relations():
    contract = {**CONTRACT, "metrics": [{"name": "m", "tables": ["returns"]}]}
    assert pick_tables(contract, AutoRunRequest(max_tables=5)) == ["sales", "store"]
    assert pick_tables(contract, AutoRunRequest(max_tables=1)) == ["sales"]
    assert pick_tables(contract, AutoRunRequest(tables=["STORE"])) == ["store"]


def test_auto_prompt_lists_relations_and_related_tables():
    system, user = metric_auto.auto_prompt(CONTRACT, CONTRACT["tables"][0], [], 3)
    assert "최대 3개" in system
    assert "sales.store_id = store.store_id" in user
    assert "- store" in user
    assert "returns" not in user  # sales 와 이어지지 않은 테이블은 싣지 않는다


class FakeCursor:
    """실행한 SQL 을 남기고, 최근 시각 조회에는 latest 를 돌려준다"""

    def __init__(self, latest):
        self.latest = latest
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchone(self):
        return (self.latest,)


def use_cursor(monkeypatch, cur, scanned=()):
    @contextmanager
    def read_only(row, timeout_s=60):
        yield cur

    monkeypatch.setattr(metric_auto, "read_only", read_only)
    # scanned 에 든 컬럼으로 정렬하는 조회는 실행 계획이 전체를 훑는다고 본다
    monkeypatch.setattr(
        metric_auto, "full_scans", lambda c, d, sql, params=None: ["sales"] if any(f'BY "{s}"' in sql for s in scanned) else []
    )


ALERT = {"name": "bms_alert", "columns": [
    {"name": "vehicle_id", "type": "integer"},
    {"name": "alert_date", "type": "date"},
    {"name": "collect_datetime", "type": "timestamp without time zone"},
]}


def test_period_example_reads_latest_through_time_index(monkeypatch):
    cur = FakeCursor(datetime.datetime(2026, 10, 20, 9, 50))
    use_cursor(monkeypatch, cur)
    indexes = [{"name": "ix_v", "columns": ["vehicle_id"]}, {"name": "ix_d", "columns": ["alert_date"]},
               {"name": "ix_t", "columns": ["collect_datetime", "vehicle_id"]}]
    period = metric_auto.period_example(PG, ALERT, indexes)
    # 시각 컬럼이 날짜 컬럼보다 먼저, 최근 날을 끝으로 3일
    assert period == {"column": "collect_datetime", "latest": "2026-10-20 09:50:00", "start": "2026-10-18", "end": "2026-10-21"}
    assert cur.executed == ['SELECT "collect_datetime" FROM "bms_alert" WHERE "collect_datetime" IS NOT NULL ORDER BY "collect_datetime" DESC LIMIT 1']


def test_period_example_skips_full_scan_and_missing_index(monkeypatch):
    cur = FakeCursor(datetime.date(2026, 10, 20))
    use_cursor(monkeypatch, cur, scanned=["collect_datetime"])
    indexes = [{"name": "ix_t", "columns": ["collect_datetime"]}, {"name": "ix_d", "columns": ["alert_date"]}]
    assert metric_auto.period_example(PG, ALERT, indexes)["column"] == "alert_date"
    assert metric_auto.period_example(PG, ALERT, [{"name": "ix_v", "columns": ["vehicle_id"]}]) is None


def test_auto_prompt_puts_period_on_index_column():
    period = {"column": "collect_datetime", "latest": "2026-10-20 09:50:00", "start": "2026-10-18", "end": "2026-10-21"}
    system, user = metric_auto.auto_prompt({**CONTRACT, "tables": [ALERT]}, ALERT, [], 3, period)
    assert "[기간 예시] 대로 bms_alert.collect_datetime 에 적습니다" in system
    assert "기간 조건은 넣지 않습니다" not in system
    assert "bms_alert.collect_datetime >= '2026-10-18' AND bms_alert.collect_datetime < '2026-10-21'" in user
    system, _ = metric_auto.auto_prompt(CONTRACT, CONTRACT["tables"][0], [], 3)
    assert "기간 조건은 넣지 않습니다" in system


BMS = {
    "name": "dev",
    "driver": "postgresql",
    "tables": [
        {"name": "bms_alert", "description": "", "purpose": "", "columns": [
            {"name": "vehicle_id", "type": "integer", "description": ""},
            {"name": "collect_datetime", "type": "timestamp without time zone", "description": "", "utc_offset": 9},
            {"name": "alert_m", "type": "text", "description": ""},
            {"name": "eg_rpm", "type": "integer", "description": ""},
            {"name": "op_mode_m", "type": "text", "description": ""},
            {"name": "uman_time", "type": "integer", "description": "", "needs_calc": True},
        ]},
        *CONTRACT["tables"],
        {"name": "item", "description": "", "purpose": "", "columns": [
            {"name": "item_id", "type": "integer", "description": ""},
            {"name": "price", "type": "numeric", "description": ""},
        ]},
    ],
    "relations": [],
    "metrics": [],
}
PERIOD = {"column": "collect_datetime", "latest": "", "start": "2026-10-18", "end": "2026-10-21"}
IN_PERIOD = "WHERE collect_datetime >= '2026-10-18' AND collect_datetime < '2026-10-21'"


def test_simple_aggregates_are_skipped():
    simple = metric_auto._simple_check(BMS, PERIOD)
    for sql in (
        # 방금 등록됐던 세 지표
        f"SELECT vehicle_id, COUNT(alert_m) AS cnt FROM bms_alert {IN_PERIOD} AND alert_m IS NOT NULL GROUP BY vehicle_id",
        f"SELECT vehicle_id, AVG(eg_rpm) AS rpm FROM bms_alert {IN_PERIOD} GROUP BY vehicle_id",
        "SELECT DATE(collect_datetime + INTERVAL '9 hours') AS day, COUNT(*) AS cnt FROM bms_alert "
        f"{IN_PERIOD} GROUP BY DATE(collect_datetime + INTERVAL '9 hours')",
        # 묶을 컬럼을 가져오는 JOIN 도 단순 집계다
        "SELECT store.name, SUM(sales.amount) AS total FROM sales JOIN store ON sales.store_id = store.store_id GROUP BY store.name",
    ):
        assert simple(sql) == metric_auto.SIMPLE_REASON, sql


def test_business_calculations_are_kept():
    simple = metric_auto._simple_check(BMS, PERIOD)
    for sql in (
        f"SELECT vehicle_id, SUM(CASE WHEN op_mode_m = '무인' THEN 1 ELSE 0 END) AS cnt FROM bms_alert {IN_PERIOD} GROUP BY vehicle_id",
        f"SELECT COUNT(DISTINCT vehicle_id) * 1.0 / NULLIF(COUNT(*), 0) AS ratio FROM bms_alert {IN_PERIOD}",
        f"SELECT vehicle_id, COUNT(*) AS cnt FROM bms_alert {IN_PERIOD} AND op_mode_m = '무인' GROUP BY vehicle_id",
        f"SELECT vehicle_id, MAX(uman_time) - MIN(uman_time) AS used FROM bms_alert {IN_PERIOD} GROUP BY vehicle_id",
        f"SELECT vehicle_id, SUM(uman_time) AS used FROM bms_alert {IN_PERIOD} GROUP BY vehicle_id",
        f"SELECT vehicle_id, COUNT(*) AS cnt FROM bms_alert {IN_PERIOD} GROUP BY vehicle_id HAVING COUNT(*) > 10",
        "SELECT store.name, SUM(sales.amount * item.price) AS cost FROM sales "
        "JOIN store ON sales.store_id = store.store_id JOIN item ON sales.store_id = item.item_id GROUP BY store.name",
    ):
        assert simple(sql) is None, sql
    # 옮기지 못하는 SQL 은 등록 확인이 알린다
    assert simple("SELECT a FROM t UNION SELECT a FROM t") is None


def test_simple_candidate_is_skipped_before_running(monkeypatch):
    setup(monkeypatch, [])
    item = _candidate("sid", PG, FakeModel(), [], proposal(), Step("t"), set(), "sales", False, lambda sql: "단순")
    assert (item["status"], item["stage"], item["reason"]) == ("skipped", "simple", "단순")
    assert item["tries"] == []


# ── 검증 ─────────────────────────────────────────────────────────────────


def metric_row(mid, name, sql=SQL, status="active"):
    return {
        "id": mid, "name": name, "description": "설명", "synonyms": [], "source_sql": sql,
        "ast": parse_sql(sql, CONTRACT)["ast"], "status": status, "broken_reason": None, "version": 1,
    }


def test_validate_without_db_and_llm_runs_static_checks():
    m = metric_row("a", "일별 매출")
    other = metric_row("b", "매출 합계")
    compiler = Compiler(CONTRACT)
    compiled_all = {
        "a": {"name": "일별 매출", "sql": compiler.compile(m["ast"])},
        "b": {"name": "매출 합계", "sql": compiler.compile(other["ast"])},
    }
    checks = validate_metric("sid", m, CONTRACT, compiler, compiled_all, None, None, Step("t"))
    by_key = {}
    for c in checks:
        by_key[c["key"]] = c
    assert by_key["compile"]["status"] == "pass"
    assert by_key["roundtrip"]["status"] == "pass"
    assert by_key["duplicate"]["status"] == "warn"
    assert "매출 합계" in by_key["duplicate"]["message"]
    for key in ("plan", "verify", "result", "llm"):
        assert by_key[key]["status"] == "skip"
    assert overall(checks)[0] == "warn"


def test_validate_reports_compile_failure_and_skips_db():
    m = metric_row("a", "일별 매출")
    m["ast"] = {**m["ast"], "from": "gone"}
    compiler = Compiler(CONTRACT)
    checks = validate_metric("sid", m, CONTRACT, compiler, {}, PG, None, Step("t"))
    assert checks[0]["status"] == "fail"
    verdict, summary = overall(checks)
    assert verdict == "fail"
    assert summary.startswith("컴파일: 지금 스키마로 컴파일되지 않습니다")
    for c in checks:
        if c["key"] in ("plan", "verify", "result"):
            assert c["status"] == "skip"


def test_llm_review_verdict_and_issues(monkeypatch):
    model = FakeModel({"verdict": "fail", "summary": "JOIN 으로 합계가 부풉니다",
                       "issues": [{"severity": "fail", "message": "store 를 JOIN 하면 중복됩니다"}, {"message": ""}]})
    check = metric_validate._review(model, CONTRACT, metric_row("a", "일별 매출"), None, Step("t"))
    assert check["status"] == "fail"
    assert check["message"] == "JOIN 으로 합계가 부풉니다"
    assert check["detail"]["issues"] == [{"severity": "fail", "message": "store 를 JOIN 하면 중복됩니다"}]
    assert "[쓰는 테이블]" in model.calls[0][1].content


def test_validation_needs_picked_metrics():
    import pytest
    from models import ApiException, ValidationRequest

    with pytest.raises(ApiException) as error:
        metric_validate.start_validation("sid", ValidationRequest(metric_ids=[]), None)
    assert error.value.status_code == 400
    assert error.value.detail == "검증할 지표를 고르십시오"
