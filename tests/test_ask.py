import datetime
import json

from langchain_core.messages import AIMessage

from ask import run_question
from test_compiler import CONTRACT

NOW = datetime.datetime(2026, 10, 15, 9, 0)

METRIC_AST = {
    "from": "bms_log",
    "select": [
        {"expr": {"fn": "DATE", "args": [{"col": "collect_datetime"}]}, "as": "day"},
        {"expr": {"col": "vehicle_id"}, "as": "vehicle_id"},
        {
            "expr": {"fn": "SUB", "args": [{"fn": "MAX", "args": [{"col": "uman_time"}]}, {"fn": "MIN", "args": [{"col": "uman_time"}]}]},
            "as": "uman",
        },
    ],
    "group_by": [{"fn": "DATE", "args": [{"col": "collect_datetime"}]}, {"col": "vehicle_id"}],
}


def contract():
    tables = [dict(t) for t in CONTRACT["tables"]]
    tables[0] = {
        **tables[0],
        "columns": [{**c, "needs_calc": True} if c["name"] == "uman_time" else c for c in tables[0]["columns"]],
    }
    return {
        **CONTRACT,
        "name": "테스트",
        "tables": tables,
        "relations": [],
        "metrics": [
            {
                "name": "무인 가동시간",
                "description": "차량별 일별 무인 가동시간 (누적값의 MAX - MIN)",
                "tables": ["bms_log"],
                "sql": "select ...",
                "ast": METRIC_AST,
            }
        ],
    }


class FakeModel:
    """정해 둔 답을 차례로 돌려주고, 받은 메시지를 남긴다"""

    model_name = "fake"

    def __init__(self, *replies):
        self.replies = [json.dumps(r, ensure_ascii=False) for r in replies]
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return AIMessage(self.replies.pop(0))


def test_needs_metric_without_metric_stops_before_sql():
    model = FakeModel(
        {"tables": ["bms_log"], "needs_metric": True, "metrics": [], "reason": "누적값 uman_time 의 기간 합계", "clarify": ""}
    )
    out = run_question({**contract(), "metrics": []}, "최근 5일 무인 가동시간 합계", model, now=NOW)
    assert out["needs_metric"] is True
    assert out["sql"] is None
    assert out["error"] == "지표 등록이 필요합니다 — 누적값 uman_time 의 기간 합계"
    # 테이블 선택 프롬프트에 계산 필요 표시가 실린다
    assert "uman_time [계산 필요]" in model.calls[0][1].content
    assert len(model.calls) == 1


def test_metric_ast_is_given_and_conditions_are_changed():
    changed = {**METRIC_AST, "where": [{"expr": {"col": "collect_datetime"}, "op": ">=", "value": "2026-10-10"}]}
    model = FakeModel(
        {"tables": ["bms_log"], "needs_metric": True, "metrics": ["무인 가동시간"], "reason": "", "clarify": ""},
        {"ast": changed, "reason": "기간만 추가"},
    )
    out = run_question(contract(), "최근 5일 일별 차량별 무인 가동시간", model, now=NOW)
    assert out["metrics"] == ["무인 가동시간"]
    assert "[지표] 무인 가동시간" in model.calls[1][1].content
    assert "계산 부분(select 의 식)은 그대로" in model.calls[1][1].content
    assert "\"collect_datetime\" >= (TIMESTAMP '2026-10-10 00:00:00' - INTERVAL '9 hours')" in out["sql"]
    assert [s["stage"] for s in out["steps"]] == ["table", "sql"]


def test_several_metrics_are_all_given_to_ast_step():
    man_ast = {
        **METRIC_AST,
        "select": [
            METRIC_AST["select"][0],
            METRIC_AST["select"][1],
            {
                "expr": {"fn": "SUB", "args": [{"fn": "MAX", "args": [{"col": "eg_time"}]}, {"fn": "MIN", "args": [{"col": "eg_time"}]}]},
                "as": "eg",
            },
        ],
    }
    base = contract()
    base["metrics"] = base["metrics"] + [
        {"name": "엔진 가동시간", "description": "차량별 일별 전체 가동시간", "tables": ["bms_log"], "sql": "select ..", "ast": man_ast}
    ]
    combined = {**METRIC_AST, "select": METRIC_AST["select"] + [man_ast["select"][2]]}
    model = FakeModel(
        {"tables": ["bms_log"], "needs_metric": True, "metrics": ["무인 가동시간", "엔진가동시간"], "reason": "", "clarify": ""},
        {"ast": combined},
    )
    out = run_question(base, "일별 무인 가동시간과 엔진 가동시간", model, now=NOW)
    assert out["metrics"] == ["무인 가동시간", "엔진 가동시간"]
    prompt = model.calls[1][1].content
    assert "[지표] 무인 가동시간" in prompt and "[지표] 엔진 가동시간" in prompt
    assert "[지표 여러 개]" in prompt
    assert out["steps"][0]["selected"] == ["bms_log", "지표: 무인 가동시간", "지표: 엔진 가동시간"]
    assert out["sql"] is not None


def test_unknown_metric_name_is_asked_again():
    model = FakeModel(
        {"tables": ["bms_log"], "needs_metric": True, "metrics": ["없는 지표"], "reason": "", "clarify": ""},
        {"tables": ["bms_log"], "needs_metric": True, "metrics": [], "reason": "유인 가동시간 지표 없음", "clarify": ""},
    )
    out = run_question(contract(), "유인 가동시간", model, now=NOW)
    assert "'없는 지표' 는 [지표] 에 없는 지표 이름입니다" in model.calls[1][-1].content
    assert out["needs_metric"] is True
    assert out["error"] == "지표 등록이 필요합니다 — 유인 가동시간 지표 없음"


def test_simple_query_builds_ast_and_retries_on_compile_error():
    bad = {"from": "bms_log", "select": [{"expr": {"col": "nope"}, "as": "x"}]}
    good = {"from": "bms_log", "select": [{"expr": {"col": "vehicle_id"}, "as": "vehicle_id"}], "distinct": True}
    model = FakeModel(
        {"tables": ["bms_log"], "needs_metric": False, "metrics": [], "reason": "목록", "clarify": ""},
        {"ast": bad},
        {"ast": good},
    )
    out = run_question(contract(), "차량 목록", model, now=NOW)
    assert out["sql"] == 'SELECT DISTINCT "vehicle_id" AS "vehicle_id" FROM "bms_log"'
    assert out["attempts"] == 2
    assert "없는 컬럼입니다 -> nope" in model.calls[2][-1].content


def test_clarify_from_table_step():
    model = FakeModel({"tables": [], "needs_metric": False, "metrics": [], "reason": "", "clarify": "어느 차량인가요?"})
    out = run_question(contract(), "그거 보여줘", model, now=NOW)
    assert out["clarification"] == "어느 차량인가요?"
    assert out["sql"] is None


def test_clarify_after_failed_attempt_clears_error():
    model = FakeModel(
        {"tables": ["bms_log"], "needs_metric": False, "metrics": [], "reason": "", "clarify": ""},
        {"ast": {"from": "bms_log", "select": [{"expr": {"col": "nope"}, "as": "x"}]}},
        {"clarify": "어느 기간인가요?"},
    )
    out = run_question(contract(), "무인 기록", model, now=NOW)
    assert out["clarification"] == "어느 기간인가요?"
    assert out["error"] is None
