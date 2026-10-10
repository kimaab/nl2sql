"""TO-BE 선택 흐름: 테이블 추론(LLM) → 지표 후보 조회(테이블 연결) → 지표 선택(LLM) → 조회 명세."""
import json

from langchain_core.messages import AIMessage

from ask import run_question
from compiler import Compiler
from contract import metric_tables
from evaluate import judge
from selector import llm_select, metric_candidates, parse_json


class FakeLLM:
    """도구 없이 부르면 replies 를 차례로(JSON 문자열), bind_tools 뒤에는 calls 를 차례로(도구 호출) 낸다."""

    def __init__(self, replies, calls=()):
        self.replies = list(replies)
        self.calls = list(calls)
        self.prompts = []
        self.model_name = "fake"

    def invoke(self, messages):
        self.prompts.append(messages)
        reply = self.replies.pop(0)
        return AIMessage(content=reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False),
                         usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})

    def bind_tools(self, tools):
        outer = self

        class Bound:
            def invoke(self, messages):
                name, args = outer.calls.pop(0)
                return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "c1"}])
        return Bound()


def tables_reply(*names):
    return {"tables": [{"name": n, "reason": "질문과 관련"} for n in names]}


def test_full_pipeline_picks_tables_then_metric_then_compiles(contract):
    model = FakeLLM(
        [tables_reply("tb_order"), {"metrics": ["매출"], "reason": "매출 = 취소 뺀 주문 금액"}],
        [("compile_sql", {"ast": json.dumps({"metric": "매출"})})],
    )
    out = run_question(contract, "장사 얼마나 됐어", model)
    assert out["tables"] == ["TB_ORDER"] and out["metrics"] == ["매출"]
    assert out["sql"].startswith("SELECT SUM(") and out["attempts"] == 1
    assert [s["stage"] for s in out["steps"]] == ["table", "metric", "sql"]
    assert out["total_tokens"] == 220  # 선택 두 번의 토큰 (가짜 모델의 도구 호출에는 사용량이 없다)
    # 지표 선택에는 고른 테이블에 연결된 지표만, 이름·설명만 간다 (정의·컬럼은 안 간다)
    metric_prompt = model.prompts[1][1].content
    assert "주문수" in metric_prompt and "TOTAL_AMT" not in metric_prompt
    assert out["steps"][1]["candidates"] == [m["name"] for m in metric_candidates(contract, ["TB_ORDER"])]


def test_ast_that_ignores_chosen_metric_is_sent_back_once(contract):
    """지표를 고른 뒤 원본 컬럼을 직접 집계하면 오류 없이 틀린 답이 나온다 — 첫 시도는 되돌려 보낸다."""
    raw = {"target_table": "TB_ORDER", "aggregations": [{"field": "TOTAL_AMT", "function": "SUM"}]}
    model = FakeLLM(
        [tables_reply("TB_ORDER"), {"metrics": ["매출"]}],
        [("compile_sql", {"ast": json.dumps(raw)}), ("compile_sql", {"ast": json.dumps({"metric": "매출"})})],
    )
    out = run_question(contract, "매출 얼마야", model)
    assert out["attempts"] == 2 and out["ast"] == {"metric": "매출"}
    # 지표 정의(고정 필터 포함)대로 전개된 SQL
    assert out["sql"] == Compiler(contract).compile({"metric": "매출"})


def test_model_may_insist_on_raw_query_after_one_warning(contract):
    raw = {"target_table": "TB_ORDER", "columns": ["ORDER_ID"]}
    model = FakeLLM([tables_reply("TB_ORDER"), {"metrics": ["주문수"]}],
                    [("compile_sql", {"ast": json.dumps(raw)}), ("compile_sql", {"ast": json.dumps(raw)})])
    out = run_question(contract, "주문 번호 목록", model)
    assert out["attempts"] == 2 and out["sql"] == 'SELECT "ORDER_ID" FROM "TB_ORDER";'


def test_table_names_are_matched_and_unknown_ones_dropped(contract):
    model = FakeLLM([{"tables": [{"name": "TB_MEMBER"}, {"name": "없는테이블"}, "tb_member"]},
                     {"metrics": []}])
    sel = llm_select(model, contract, "VIP 회원 목록")
    assert sel.tables == ["TB_MEMBER"] and sel.metrics == []
    # 지표가 없으면 테이블을 직접 조회하도록 테이블만 싣는다
    assert [t["name"] for t in sel.pruned["tables"]] == ["TB_MEMBER"] and sel.pruned["metrics"] == []


def test_no_table_means_clarification(contract):
    sel = llm_select(FakeLLM([{"tables": []}]), contract, "오늘 날씨 어때")
    assert sel.clarification and "테이블을 찾지 못했습니다" in sel.clarification
    out = run_question(contract, "오늘 날씨 어때", FakeLLM([{"tables": []}]))
    assert out["clarification"] and out["sql"] is None and [s["stage"] for s in out["steps"]] == ["table"]


def test_ambiguous_metric_asks_back_with_options(contract):
    model = FakeLLM([tables_reply("TB_ORDER"), {"metrics": [], "ambiguous": True, "options": ["주문수", "취소주문수"],
                                                "message": "전체 주문 수인가요, 취소된 주문 수인가요?"}])
    out = run_question(contract, "주문 건수", model)
    assert out["sql"] is None and "후보: 주문수, 취소주문수" in out["clarification"]


def test_bad_json_is_retried_once(contract):
    model = FakeLLM(["표를 고르면 TB_ORDER 입니다", tables_reply("TB_ORDER"), {"metrics": ["주문수"]}])
    sel = llm_select(model, contract, "주문 수")
    assert sel.metrics == ["주문수"] and "형식이 틀렸습니다" in model.prompts[1][-1].content
    assert sel.steps[0].prompt_tokens == 200  # 두 번 물은 토큰이 다 잡힌다


def test_parse_json_accepts_fences_and_chatter():
    assert parse_json('네.\n```json\n{"tables": []}\n```') == {"tables": []}
    assert parse_json('답: {"metrics": ["a"]} 입니다') == {"metrics": ["a"]}


def test_metric_tables_from_definition():
    by_name = {"주문수": {"table_name": "TB_ORDER"}, "환불": {"table_name": "TB_REFUND"}}
    m = {"table_name": "TB_ORDER", "kind": "derived", "expression": "[환불] / [주문수]",
         "joins": [{"table": "TB_MEMBER"}]}
    assert metric_tables(m, by_name) == [("TB_ORDER", "base"), ("TB_MEMBER", "join"), ("TB_REFUND", "join")]


def test_judge_per_stage():
    case = {"expected_tables": ["TB_ORDER"], "expected_metric": "매출", "expected_sql": 'SELECT SUM(x) AS "a" FROM t;'}
    hit = judge({"tables": ["TB_ORDER", "TB_MEMBER"], "metrics": ["매출"], "sql": 'SELECT SUM(x) AS "b" FROM t;'}, case)
    assert hit == {"table_hit": True, "metric_hit": True, "sql_hit": True}
    miss = judge({"tables": ["TB_MEMBER"], "metrics": [], "sql": None}, {**case, "expected_metric": None})
    assert miss == {"table_hit": False, "metric_hit": True, "sql_hit": False}  # '지표 없음' 이 정답이면 안 고른 게 맞다
