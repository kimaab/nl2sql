"""TO-BE 선택 흐름: 지표 선택(LLM) → 조회 명세 (지표가 테이블·계산식을 정한다)."""

import json

from langchain_core.messages import AIMessage

from ask import run_question, system_prompt
from contract import metric_tables
from evaluate import judge
from selector import llm_select, parse_json
from test_formulas import FORMULAS, METRIC


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
        return AIMessage(
            content=reply
            if isinstance(reply, str)
            else json.dumps(reply, ensure_ascii=False),
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
            },
        )

    def bind_tools(self, tools):
        outer = self

        class Bound:
            def invoke(self, messages):
                name, args = outer.calls.pop(0)
                return AIMessage(
                    content="", tool_calls=[{"name": name, "args": args, "id": "c1"}]
                )

        return Bound()


STATE = {
    "name": "장비 상태",
    "description": "장비 상태 기록 (경고·충전)",
    "table": "bms_condition",
    "dimensions": [],
    "fixed_filters": [],
    "formulas": [],
    "tables": ["bms_condition"],
}

CONTRACT = {
    "name": "BARONESS",
    "domain": "무인 장비 운행",
    "driver": "postgresql",
    "relations": [],
    "formulas": FORMULAS,
    "metrics": [{**METRIC, "tables": ["bms_log"], "synonyms": ["가동"]}, STATE],
    "tables": [
        {
            "name": "bms_log",
            "description": "장비 누적 기록",
            "columns": [
                {"name": "collect_datetime", "type": "timestamp", "utc_offset": 9},
                {"name": "vehicle_id", "type": "varchar(20)"},
                {"name": "uman_time", "type": "numeric", "description": "무인 가동시간 누적"},
                {"name": "man_time", "type": "numeric"},
                {"name": "volt_max", "type": "numeric"},
                {"name": "volt_min", "type": "numeric"},
            ],
        },
        {
            "name": "bms_condition",
            "description": "장비 상태",
            "columns": [
                {"name": "collect_datetime", "type": "timestamp"},
                {"name": "vehicle_id", "type": "varchar(20)"},
                {"name": "alarm_code", "type": "varchar(10)"},
            ],
        },
    ],
}

Q1 = "최근 5일 일별 계산된 무인 가동시간"


def test_pipeline_picks_metric_then_compiles_with_formula():
    ast = {
        "metric": "장비 가동",
        "aggregations": [{"formula": "무인 가동시간", "function": "SUM"}],
        "group_by": [{"field": "collect_datetime", "grain": "day"}],
    }
    model = FakeLLM(
        [{"metric": "장비 가동", "reason": "가동시간 계산식"}],
        [("compile_sql", {"ast": json.dumps(ast, ensure_ascii=False)})],
    )
    out = run_question(CONTRACT, Q1, model)
    assert out["metrics"] == ["장비 가동"] and out["tables"] == ["bms_log"]
    assert 'SUM("_m1") AS "무인 가동시간"' in out["sql"] and out["attempts"] == 1
    assert [s["stage"] for s in out["steps"]] == ["metric", "sql"]
    # 지표 선택에는 지표마다 설명·같은 말·테이블·계산식이 간다 (컬럼은 안 간다)
    prompt = model.prompts[0][1].content
    assert "계산식: 무인 가동시간" in prompt and "같은 말: 가동" in prompt
    assert "테이블: bms_condition" in prompt and "uman_time" not in prompt


def test_metric_without_mentioned_formula_is_judged_again():
    model = FakeLLM([{"metric": "장비 상태"}, {"metric": "장비 가동"}])
    sel = llm_select(model, CONTRACT, Q1)
    assert sel.metrics == ["장비 가동"] and sel.clarification is None
    again = model.prompts[1][1].content
    assert "[다시 고르기]" in again and "지표명: 장비 상태" not in again
    # 고른 지표의 테이블과 허용 계산식만 싣는다
    assert [t["name"] for t in sel.pruned["tables"]] == ["bms_log"]
    assert {f["name"] for f in sel.pruned["formulas"]} == set(METRIC["formulas"])


def test_no_metric_allows_the_formula_asks_back_without_llm():
    contract = {**CONTRACT, "metrics": [STATE]}
    model = FakeLLM([])
    sel = llm_select(model, contract, Q1)
    assert sel.metrics == [] and "계산할 수 있는 지표가 없습니다" in sel.clarification
    assert model.prompts == []


def test_no_metric_means_clarification():
    out = run_question(CONTRACT, "오늘 날씨 어때", FakeLLM([{"metric": None}]))
    assert out["clarification"] and "지표를 찾지 못했습니다" in out["clarification"]
    assert out["sql"] is None and [s["stage"] for s in out["steps"]] == ["metric"]


def test_ambiguous_metric_asks_back_with_options():
    model = FakeLLM(
        [
            {
                "metric": None,
                "ambiguous": True,
                "options": ["장비 가동", "장비 상태"],
                "message": "가동 기록인가요, 상태 기록인가요?",
            }
        ]
    )
    out = run_question(CONTRACT, "장비 기록", model)
    assert out["sql"] is None and "후보: 장비 가동, 장비 상태" in out["clarification"]


def test_bad_json_is_retried_once():
    model = FakeLLM(["지표는 장비 가동 입니다", {"metric": "장비 가동"}])
    sel = llm_select(model, CONTRACT, "가동 기록")
    assert sel.metrics == ["장비 가동"] and "형식이 틀렸습니다" in model.prompts[1][-1].content
    assert sel.steps[0].prompt_tokens == 200  # 두 번 물은 토큰이 다 잡힌다


def test_ast_that_ignores_chosen_metric_is_sent_back_once():
    raw = {"target_table": "bms_log", "aggregations": [{"field": "uman_time", "function": "MAX"}]}
    fixed = {"metric": "장비 가동", "aggregations": [{"field": "uman_time", "function": "MAX"}]}
    model = FakeLLM(
        [{"metric": "장비 가동"}],
        [("compile_sql", {"ast": json.dumps(raw)}), ("compile_sql", {"ast": json.dumps(fixed, ensure_ascii=False)})],
    )
    out = run_question(CONTRACT, "무인 가동시간 최댓값", model)
    assert out["attempts"] == 2 and out["ast"] == fixed
    assert out["sql"] == 'SELECT MAX("uman_time") AS "max_uman_time" FROM "bms_log";'


def test_forced_formula_without_mention_is_sent_back():
    """'계산된' 없이 물으면 컬럼 그대로 — 강제 계산식을 쓰면 되돌려 보낸다"""
    wrong = {"metric": "장비 가동", "aggregations": [{"formula": "무인 가동시간"}]}
    right = {"metric": "장비 가동", "aggregations": [{"field": "uman_time", "function": "MAX"}]}
    model = FakeLLM(
        [{"metric": "장비 가동"}],
        [
            ("compile_sql", {"ast": json.dumps(wrong, ensure_ascii=False)}),
            ("compile_sql", {"ast": json.dumps(right, ensure_ascii=False)}),
        ],
    )
    out = run_question(CONTRACT, "무인 가동시간 최댓값", model)
    assert out["attempts"] == 2 and out["ast"] == right


def test_sql_prompt_lists_allowed_formulas_and_rules():
    sel = llm_select(FakeLLM([{"metric": "장비 가동"}]), CONTRACT, Q1)
    prompt = system_prompt(CONTRACT, sel.pruned, chosen_metrics=sel.metrics)
    assert "[계산식]" in prompt and "'계산된 <이름>'" in prompt
    assert '"식": "ABS(MAX(uman_time) - MIN(uman_time))"' in prompt
    assert '"식": "[무인 가동시간] / ([무인 가동시간] + [유인 가동시간])"' in prompt
    assert "collect_datetime(day), vehicle_id 단위로 계산됩니다" in prompt


def test_parse_json_accepts_fences_and_chatter():
    assert parse_json('네.\n```json\n{"metric": null}\n```') == {"metric": None}
    assert parse_json('답: {"metric": "a"} 입니다') == {"metric": "a"}


def test_metric_tables_from_definition():
    m = {"table_name": "TB_ORDER", "joins": [{"table": "TB_MEMBER"}, {"table": "tb_order"}]}
    assert metric_tables(m) == [("TB_ORDER", "base"), ("TB_MEMBER", "join")]


def test_judge_per_stage():
    case = {
        "expected_tables": ["TB_ORDER"],
        "expected_metric": "매출",
        "expected_sql": 'SELECT SUM(x) AS "a" FROM t;',
    }
    hit = judge(
        {
            "tables": ["TB_ORDER", "TB_MEMBER"],
            "metrics": ["매출"],
            "sql": 'SELECT SUM(x) AS "b" FROM t;',
        },
        case,
    )
    assert hit == {"table_hit": True, "metric_hit": True, "sql_hit": True}
    miss = judge(
        {"tables": ["TB_MEMBER"], "metrics": [], "sql": None},
        {**case, "expected_metric": None},
    )
    assert miss == {
        "table_hit": False,
        "metric_hit": True,
        "sql_hit": False,
    }  # '지표 없음' 이 정답이면 안 고른 게 맞다


def test_metric_named_by_description_or_spacing_is_resolved():
    # 이름 대신 설명을 적거나 띄어쓰기가 달라도 그 지표로 본다
    assert llm_select(FakeLLM([{"metric": "장비 상태 기록 (경고·충전)"}]), CONTRACT, "경고 기록").metrics == ["장비 상태"]
    assert llm_select(FakeLLM([{"metric": "장비가동"}]), CONTRACT, "가동 기록").metrics == ["장비 가동"]


def test_unknown_metric_name_is_asked_again_not_clarified():
    model = FakeLLM([{"metric": "기간별 장비 가동"}, {"metric": "장비 가동"}])
    sel = llm_select(model, CONTRACT, "가동 기록")
    assert sel.metrics == ["장비 가동"] and sel.clarification is None
    assert "에 없는 지표명입니다" in model.prompts[1][-1].content
