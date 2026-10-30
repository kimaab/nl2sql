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


def test_missing_metric_is_returned_and_wrong_entries_are_dropped():
    model = FakeModel(
        {
            "tables": ["bms_log"],
            "needs_metric": True,
            "metrics": [],
            "missing": [
                {"name": "무인 절단 시간", "table": "BMS_LOG", "column": "UMAN_TIME"},
                {"name": "없는 테이블", "table": "nope", "column": "x"},
                {"name": "컬럼 모름", "table": "bms_log", "column": "nope"},
                {"table": "bms_log"},
            ],
            "reason": "uman_time 지표 없음",
            "clarify": "",
        }
    )
    out = run_question({**contract(), "metrics": []}, "최근 7일 차량별 무인 절단 시간 합계", model, now=NOW)
    assert out["needs_metric"] is True
    assert out["missing"] == [
        {"name": "무인 절단 시간", "table": "bms_log", "column": "uman_time"},
        {"name": "컬럼 모름", "table": "bms_log", "column": ""},
    ]
    assert len(model.calls) == 1


def test_missing_is_empty_when_a_metric_is_used():
    model = FakeModel(
        {
            "tables": ["bms_log"],
            "needs_metric": True,
            "metrics": ["무인 가동시간"],
            "missing": [{"name": "무인 가동시간", "table": "bms_log", "column": "uman_time"}],
            "reason": "",
            "clarify": "",
        },
        {"ast": METRIC_AST},
    )
    out = run_question(contract(), "일별 차량별 무인 가동시간", model, now=NOW)
    assert out["missing"] == []
    assert out["sql"] is not None


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


def ast_reply(select, group_by=None):
    return {"ast": {"from": "bms_log", "select": select, "group_by": group_by or []}, "reason": ""}


SUM_UMAN = {"expr": {"fn": "SUM", "args": [{"col": "uman_time"}]}, "as": "uman"}
VEHICLE = {"expr": {"col": "vehicle_id"}, "as": "vehicle_id"}


def test_calc_column_without_metric_turns_into_needs_metric():
    """테이블 선택이 지표 필요를 놓쳐도, 지표 없이 [계산 필요] 컬럼을 계산하면 SQL 대신 지표 등록으로 끝난다"""
    model = FakeModel(
        {"tables": ["bms_log"], "needs_metric": False, "metrics": [], "reason": "", "clarify": ""},
        ast_reply([VEHICLE, SUM_UMAN], [{"col": "vehicle_id"}]),
    )
    out = run_question({**contract(), "metrics": []}, "차량별 무인 가동시간 합계", model, now=NOW)
    assert out["sql"] is None and out["needs_metric"] is True
    assert out["missing"] == [{"name": "uman_time", "table": "bms_log", "column": "uman_time"}]
    assert out["error"].startswith("지표 등록이 필요합니다 — bms_log.uman_time")
    assert len(model.calls) == 2  # 다시 묻지 않는다


def test_calc_column_must_follow_the_chosen_metric():
    """고른 지표가 그 컬럼을 쓰면, 지표 식이 아닌 계산은 고쳐 쓰게 한다"""
    metric_expr = METRIC_AST["select"][2]["expr"]
    model = FakeModel(
        {"tables": ["bms_log"], "needs_metric": True, "metrics": ["무인 가동시간"], "reason": "", "clarify": ""},
        ast_reply([VEHICLE, SUM_UMAN], [{"col": "vehicle_id"}]),
        ast_reply([VEHICLE, {"expr": metric_expr, "as": "uman"}], [{"col": "vehicle_id"}]),
    )
    out = run_question(contract(), "차량별 무인 가동시간", model, now=NOW)
    assert out["sql"] == (
        'SELECT "vehicle_id" AS "vehicle_id", (MAX("uman_time") - MIN("uman_time")) AS "uman" FROM "bms_log" GROUP BY "vehicle_id"'
    )
    assert out["attempts"] == 2
    assert "[계산 필요] 컬럼" in model.calls[2][-1].content


def test_user_id_reaches_both_stages():
    model = FakeModel(
        {"tables": ["bms_log"], "needs_metric": False, "metrics": [], "reason": "", "clarify": ""},
        {"ast": {"from": "bms_log", "select": [{"expr": {"col": "vehicle_id"}, "as": "v"}]}, "reason": ""},
    )
    out = run_question(contract(), "내 차량 목록", model, now=NOW, user_id="baroness-admin")
    assert out["sql"]
    for call in model.calls:
        assert "로그인한 사용자 아이디: baroness-admin" in call[-1].content
        assert "[질문]\n내 차량 목록" in call[-1].content


def test_no_user_id_keeps_question_as_is():
    model = FakeModel({"tables": [], "needs_metric": False, "metrics": [], "reason": "", "clarify": "어떤 차량인가요?"})
    run_question(contract(), "내 차량 목록", model, now=NOW)
    assert "[현재 사용자]" not in model.calls[0][-1].content


def test_analysis_is_parsed_and_given_to_ast_step():
    model = FakeModel(
        {
            "items": [{"name": "차량", "column": "bms_log.vehicle_id"}, {"name": "가동시간", "column": "EG_TIME"}],
            "tables": ["bms_log"],
            "grouping": False,
            "dimensions": [{"name": "무시됨", "column": "bms_log.vehicle_id"}],
            "conditions": [{"name": "차량", "column": "bms_log.vehicle_id", "value": ["A1", "A2"]}, {"name": "", "column": "nope.x"}],
            "ambiguous": [],
            "needs_metric": False,
            "metrics": [],
            "reason": "",
        },
        {"ast": {"from": "bms_log", "select": [{"expr": {"col": "vehicle_id"}, "as": "차량"}]}, "reason": ""},
    )
    out = run_question(contract(), "A1, A2 차량의 가동시간", model, now=NOW)
    assert out["sql"]
    assert out["analysis"]["items"] == [
        {"name": "차량", "column": "bms_log.vehicle_id"},
        {"name": "가동시간", "column": "bms_log.eg_time"},
    ]
    # 묶지 않으면 묶음 항목은 버린다. 모르는 테이블의 이름 없는 조건도 버린다
    assert out["analysis"]["dimensions"] == []
    assert out["analysis"]["conditions"] == [{"name": "차량", "column": "bms_log.vehicle_id", "value": '["A1", "A2"]'}]
    sent = model.calls[1][1].content
    assert "[질문 분석]\n- 조회 항목: 차량 (bms_log.vehicle_id), 가동시간 (bms_log.eg_time)" in sent
    assert "- 묶음: 없음" in sent
    assert '- 조건: 차량 (bms_log.vehicle_id) = ["A1", "A2"]' in sent


def test_all_columns_lists_columns_and_stops():
    described = contract()
    described["tables"][1] = {
        "name": "vehicle",
        "description": "차량 기본 정보",
        "columns": [
            {"name": "vehicle_id", "type": "varchar", "description": "차량 시리얼 번호"},
            {"name": "vehicle_name", "type": "varchar", "description": "작동모드:1 = 매뉴얼, 2 = 티치"},
            {"name": "memo", "type": "varchar"},
        ],
    }
    model = FakeModel(
        {
            "items": [{"name": "전체", "column": "vehicle.*"}],
            "tables": [],
            "grouping": False,
            "dimensions": [],
            "conditions": [],
            "ambiguous": [],
            "needs_metric": False,
            "metrics": [],
            "reason": "",
        }
    )
    out = run_question(described, "차량 정보 전체", model, now=NOW)
    assert out["sql"] is None and out["clarification"]
    assert out["tables"] == ["vehicle"]
    assert out["columns"] == [
        {
            "table": "vehicle",
            "description": "차량 기본 정보",
            "columns": [
                {"name": "vehicle_id", "label": "차량 시리얼 번호"},
                {"name": "vehicle_name", "label": "작동모드"},
                {"name": "memo", "label": "memo"},
            ],
            "text": "조회 항목(vehicle): 차량 시리얼 번호(vehicle_id), 작동모드(vehicle_name), memo",
        }
    ]
    assert len(model.calls) == 1


def test_several_ambiguous_parts_are_asked_together():
    model = FakeModel(
        {
            "items": [{"name": "가동시간", "column": ""}],
            "tables": ["bms_log"],
            "ambiguous": [
                {"part": "조회 항목", "question": "엔진 가동시간인가요, 무인 가동시간인가요?"},
                {"part": "엉뚱", "question": "어느 차량인가요?"},
                {"part": "조건", "question": ""},
            ],
            "needs_metric": False,
            "metrics": [],
            "reason": "",
        }
    )
    out = run_question(contract(), "가동시간 보여줘", model, now=NOW)
    assert out["clarification"] == "- 조회 항목: 엔진 가동시간인가요, 무인 가동시간인가요?\n- 어느 차량인가요?"
    assert len(model.calls) == 1


def test_missing_calc_stops_even_when_another_metric_is_chosen():
    """지표 하나를 골랐어도 그 지표가 하지 않는 계산이 남으면 AST 로 넘어가지 않는다"""
    model = FakeModel(
        {
            "tables": ["bms_log"],
            "needs_metric": True,
            "metrics": ["무인 가동시간"],
            "missing": [{"name": "엔진 가동시간", "table": "bms_log", "column": "eg_time"}],
            "reason": "엔진 가동시간 지표가 없음",
        }
    )
    out = run_question(contract(), "차량별 무인 가동시간과 엔진 가동시간", model, now=NOW)
    assert out["needs_metric"] is True and out["sql"] is None
    assert out["missing"] == [{"name": "엔진 가동시간", "table": "bms_log", "column": "eg_time"}]
    assert len(model.calls) == 1
