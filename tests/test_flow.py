import datetime
import json
from pathlib import Path

from langchain_core.messages import AIMessage

from ask import run_question, system_prompt
from contract import broken_reasons
from evaluate import normalize_sql, run_compile_only
from pruner import Pruner


# ------------------------------------------------------------------ T-03 평가셋 자체 점검

CASES = Path(__file__).resolve().parent.parent / "eval" / "shoppingmall_cases.json"


def test_eval_cases_compile_to_expected_sql(contract):
    data = json.loads(CASES.read_text(encoding="utf-8"))
    report = run_compile_only(contract, data)
    assert report["total"] >= 30
    assert report["failures"] == [], report["failures"][:3]


def test_normalize_sql_keeps_literals():
    assert normalize_sql("SELECT  a\n FROM t WHERE x = 'a  b'") == "SELECT a FROM t WHERE x = 'a  b'"


# ------------------------------------------------------------------ 프루닝 (A-04 동의어·코드값, A-02 예시)

def test_pruner_finds_table_by_code_label_and_synonym(contract):
    pruned = Pruner(contract).prune("VIP 고객명 알려줘", top_k=2)
    assert "TB_MEMBER" in [t["name"] for t in pruned["tables"]]


def test_pruner_brings_join_tables_and_relations(contract):
    pruned = Pruner(contract).prune("주문상세", top_k=1)
    names = [t["name"] for t in pruned["tables"]]
    assert names[:2] == ["TB_ORDER", "TB_ORDER_ITEM"]
    assert any(r["constraint"] == "FK_ITEM_ORDER" for r in pruned["relations"])


def test_prompt_has_examples_separately(contract):
    pruned = Pruner(contract).prune("이번 달 매출", top_k=3)
    prompt = system_prompt(contract, pruned, datetime.date(2026, 10, 6))
    assert "[예시]\n질문: 이번 달 매출" in prompt
    metadata = prompt.split("[허용된 메타데이터]\n", 1)[1].split("\n\n[조회 명세", 1)[0]
    assert "examples" not in metadata
    assert "2026-10-06 (화요일)" in prompt


# ------------------------------------------------------------------ 에이전트 흐름 (A-05 되묻기)

class ScriptedModel:
    """정해진 도구 호출을 차례로 내는 가짜 모델."""

    def __init__(self, calls):
        self.calls = list(calls)
        self.seen = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.seen.append(messages)
        name, args = self.calls.pop(0)
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{len(self.seen)}"}])


def test_retry_then_success_records_ast(contract):
    model = ScriptedModel([
        ("compile_sql", {"ast": json.dumps({"metric": "없는지표"})}),
        ("compile_sql", {"ast": json.dumps({"metric": "주문수"})}),
    ])
    out = run_question(contract, "주문 수", model)
    assert out["sql"] == 'SELECT COUNT(*) AS "주문수" FROM "TB_ORDER";'
    assert out["attempts"] == 2 and out["ast"] == {"metric": "주문수"}
    # 두 번째 호출은 첫 오류를 보고 있다
    assert "등록되지 않은 지표" in str(model.seen[1][-1].content)


def test_clarification_ends_without_sql(contract):
    model = ScriptedModel([("ask_user", {"message": "어느 회원의 주문상세인가요?"})])
    out = run_question(contract, "주문상세 보여줘", model)
    assert out == {"sql": None, "error": None, "attempts": 0, "ast": None,
                   "clarification": "어느 회원의 주문상세인가요?"}


def test_max_attempts_returns_last_error(contract):
    model = ScriptedModel([("compile_sql", {"ast": json.dumps({"metric": f"x{i}"})}) for i in range(4)])
    out = run_question(contract, "?", model)
    assert out["sql"] is None and out["attempts"] == 4 and "'x3'" in out["error"]


# ------------------------------------------------------------------ 깨진 지표 (T-02)

def _row(name, kind="aggregate", **kw):
    base = {"name": name, "kind": kind, "table_name": "TB_ORDER", "joins": [], "select_columns": [],
            "agg_field": "*", "agg_function": "COUNT", "expression": None, "fixed_filters": []}
    return {**base, **kw}


def _tables(contract):
    return {t["name"]: t for t in contract["tables"]}


def test_broken_projection_join_and_derived_cascade(contract):
    tables = _tables(contract)
    rows = [
        _row("ok"),
        _row("join_ok", "projection", joins=[{"table": "TB_ORDER_ITEM", "on": [
            {"left": "TB_ORDER.ORDER_ID", "right": "TB_ORDER_ITEM.ORDER_ID"}]}],
            select_columns=["TB_ORDER.ORDER_ID", "TB_ORDER_ITEM.QTY"]),
        _row("join_bad", "projection", select_columns=["TB_ORDER_ITEM.QTY"]),
        _row("gone", agg_field="NOPE", agg_function="SUM"),
        _row("ratio_bad", "derived", expression="[ok] / [gone]"),
        _row("ratio_ok", "derived", expression="[ok] * 2"),
        _row("ratio_proj", "derived", expression="[join_ok] + 1"),
    ]
    broken = broken_reasons(rows, tables)
    assert set(broken) == {"join_bad", "gone", "ratio_bad", "ratio_proj"}
    assert "gone" in broken["ratio_bad"] and "집계형이 아닙니다" in broken["ratio_proj"]


def test_join_metric_with_dropped_join_table_is_broken(contract):
    tables = _tables(contract)
    del tables["TB_ORDER_ITEM"]
    rows = [_row("j", "projection", joins=[{"table": "TB_ORDER_ITEM", "on": [
        {"left": "TB_ORDER.ORDER_ID", "right": "TB_ORDER_ITEM.ORDER_ID"}]}], select_columns=["ORDER_ID"])]
    assert "조인할 수 없는 테이블" in broken_reasons(rows, tables)["j"]


def test_loose_sql_ignores_alias_names_only():
    from evaluate import loose_sql
    a = 'SELECT "M", COUNT(*) AS "count_*" FROM "T" GROUP BY "M" ORDER BY "count_*" DESC;'
    b = 'SELECT "M", COUNT(*) AS "member_count" FROM "T" GROUP BY "M" ORDER BY "member_count" DESC;'
    c = 'SELECT "M", SUM("X") AS "member_count" FROM "T" GROUP BY "M" ORDER BY "member_count" DESC;'
    assert loose_sql(a) == loose_sql(b) != loose_sql(c)
    assert loose_sql("SELECT a, 'x, y' AS \"q\" FROM t;") == "SELECT a, 'x, y' FROM t;"


def test_pruner_adds_related_neighbour_that_matches_question(contract):
    pruned = Pruner(contract).prune("등급별 주문 금액 합계", top_k=3)
    names = [t["name"] for t in pruned["tables"]]
    assert "TB_ORDER" in names and "TB_MEMBER" in names
    assert any(r["constraint"] == "FK_ORDER_MEMBER" for r in pruned["relations"])
