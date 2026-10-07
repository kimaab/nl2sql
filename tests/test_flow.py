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


# ------------------------------------------------------------------ 의미 검색 결합 (RRF)

def test_semantic_finds_metric_that_shares_no_letters(contract):
    """글자가 하나도 안 겹치는 말투는 TF-IDF 로는 못 찾고, 의미 검색 점수가 오면 찾는다."""
    question = "장사 얼마나 됐어"
    assert Pruner(contract).prune(question)["metrics"] == []

    pruned = Pruner(contract).prune(question, semantic={"매출": 0.82})
    assert [m["name"] for m in pruned["metrics"]] == ["매출"]
    assert "TB_ORDER" in [t["name"] for t in pruned["tables"]]


def test_semantic_and_keyword_agreement_ranks_first(contract):
    """양쪽 검색에서 모두 걸린 지표가 한쪽에서만 1위인 지표보다 앞선다."""
    pruned = Pruner(contract).prune("취소 주문 건수", top_k=2, semantic={"취소주문수": 0.71, "매출": 0.9})
    assert [m["name"] for m in pruned["metrics"]][0] == "취소주문수"
    assert "매출" in [m["name"] for m in pruned["metrics"]]


def test_semantic_unknown_metric_name_is_ignored(contract):
    """깨져서 계약서에서 빠진 지표의 점수가 와도 후보가 되지 않는다."""
    plain = Pruner(contract).prune("이번 달 매출")
    mixed = Pruner(contract).prune("이번 달 매출", semantic={"없는지표": 0.99})
    assert [m["name"] for m in mixed["metrics"]] == [m["name"] for m in plain["metrics"]]


def test_semantic_below_min_is_not_a_candidate(contract):
    pruned = Pruner(contract).prune("장사 얼마나 됐어", semantic={"매출": 0.42}, semantic_min=0.5)
    assert pruned["metrics"] == []


def test_search_log_shows_keyword_semantic_and_fused_scores(contract, caplog):
    caplog.set_level("INFO", logger="nl2sql.pruner")
    Pruner(contract).prune("취소 주문 건수", top_k=2,
                           semantic={"취소주문수": 0.71, "매출": 0.62, "최근주문": 0.31}, semantic_min=0.5)
    lines = [r.getMessage() for r in caplog.records]
    row = next(line for line in lines if "[metric] 취소주문수" in line)
    assert "TF-IDF 0." in row and "의미 0.710 (1위)" in row and "결합 0." in row and "✔" in row
    below = [line for line in lines if "[metric] 최근주문" in line]
    assert len(below) == 1 and "의미 0.310 (기준 미만)" in below[0]


def test_metric_texts_and_hash_change_only_with_content():
    from embedding import _hash, metric_texts

    row = {"name": "매출", "description": "취소를 뺀 주문 금액 합계",
           "examples": [{"question": "이번 달 매출"}, {"question": " "}]}
    assert metric_texts(row) == [("definition", "매출: 취소를 뺀 주문 금액 합계"), ("example", "이번 달 매출")]
    assert metric_texts({"name": "주문수", "description": ""}) == [("definition", "주문수")]
    assert _hash("example", "이번 달 매출") == _hash("example", "이번 달 매출") != _hash("definition", "이번 달 매출")
