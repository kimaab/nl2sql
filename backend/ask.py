import datetime
import json
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from compiler import (
    GRAINS,
    MENTION_PREFIX,
    Compiler,
    by_text,
    expand_formula,
    measure_has_aggregate,
    measure_text,
)
from graph import MAX_ATTEMPTS, build_graph
from selector import Step, llm_select, model_name, tfidf_select, usage_of
from tool import CLARIFY_PREFIX, make_clarify_tool, make_compile_tool

AST_GUIDE = """[조회 명세(ast) 모양]
ast 는 JSON 객체이고 아래 키만 씁니다. 키 이름을 바꾸지 마십시오 (column 이 아니라 field).
- metric: 이번 질문의 지표 이름. 지표가 테이블·조인·고정 필터를 정하니 다시 적지 않습니다.
- columns: 행 목록을 볼 때 ["컬럼", "테이블.컬럼", {"formula": "행 단위 계산식"}]. 모든 컬럼은 ["*"]. aggregations 와 함께 쓰지 않습니다.
- latest: 단위마다 가장 마지막(최근)·처음 N건의 행을 고를 때 {"order_by": "순서 컬럼", "direction": "desc", "count": 1}.
  columns 와 함께 쓰고, 단위는 group_by 에 적습니다 (예: 일별 장비별 마지막 데이터 → group_by [{"field": 날짜, "grain": "day"}, "장비"]).
  group_by 를 비우면 지표의 디멘젼이 단위입니다. 행 목록에서 group_by 는 latest 와 함께일 때만 씁니다.
- aggregations: 집계할 때의 목록. 항목은 셋 중 하나입니다.
  {"field": "컬럼 또는 *", "function": "COUNT", "alias": "선택"} — function 은 SUM, COUNT, AVG, MIN, MAX, COUNT_DISTINCT 중 하나이고 괄호 없이 이름만 적습니다.
  {"formula": "집계 계산식", "alias": "선택"} — 계산식을 지표의 디멘젼 단위로 계산한 뒤 group_by 단위로 합칩니다
  (예: 디멘젼이 일×장비인 가동시간을 group_by [날짜] 로 물으면 날짜별 전체 합계). group_by 를 비우면 디멘젼별로 나옵니다.
  {"formula": "집계 계산식", "function": "AVG"} — 디멘젼별 값을 합계가 아닌 함수로 group_by 단위까지 다시 집계할 때
  (예: 장비별 하루 평균 가동시간 → function AVG, group_by ["장비"]).
  행 단위 계산식은 {"formula": "…", "function": "AVG"} 처럼 function 이 필요합니다.
- filters: [{"field": "컬럼", "operator": "equals", "value": "값"}] — 모두 AND 로 묶입니다.
  operator: equals, not_equals, greater_than, greater_or_equal, less_than, less_or_equal,
  in, not_in (value 는 목록), is_null, is_not_null (value 없음), contains, starts_with, ends_with (문자열 일부 일치).
  OR 는 {"any_of": [조건, 조건]} 으로, 그 안의 AND 묶음은 {"all_of": [조건, 조건]} 으로 적습니다.
- group_by: ["컬럼"]. 날짜를 기간 단위로 묶을 때는 {"field": "날짜컬럼", "grain": "hour|day|week|month|quarter|year"} — 결과 별칭은 "컬럼_grain" (예: ORDER_DTM_month).
- having: 집계 결과 조건 [{"alias": "집계 별칭", "operator": "greater_or_equal", "value": 3}].
- order_by: [{"field": "컬럼 또는 집계 별칭", "direction": "asc|desc"}]. 집계 질의는 group_by 컬럼이나 집계 별칭으로만 정렬합니다.
  계산식의 결과 이름은 alias 가 없으면 계산식 이름입니다.
- limit: 1~1000 정수. "상위 10개", "최근 5건" 은 order_by 와 limit 을 함께 씁니다.
- distinct: true — 행 목록의 중복을 뺄 때. 집계의 중복 제거는 COUNT_DISTINCT 입니다.
- compare: 기간 비교 {"field": "날짜컬럼", "periods": [{"label": "이번달", "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}, {"label": "지난달", "from": "...", "to": "..."}]}.
  첫 기간이 기준이고, 기간이 둘이면 증감률이 함께 나옵니다. 집계에만 씁니다."""

FORMULA_GUIDE = """[계산식]
계산식은 데이터를 보지 않으면 알 수 없는 계산을 미리 정해 둔 것입니다 (예: 누적값의 하루 증가분).
- 질문에 '{prefix} <이름>' 이 있으면 그 계산식을 {{"formula": "<이름>"}} 으로 씁니다.
- '{prefix}' 없이 이름만 나오면 계산식이 아니라 컬럼을 그대로 씁니다. 단 mode 가 organic 인 계산식은 질문 뜻에 맞으면 써도 됩니다.
- 계산식의 컬럼·식을 원본 컬럼으로 다시 풀어 적지 마십시오."""


def _prompt_metric(m: dict) -> dict:
    """프롬프트에 싣는 지표: 테이블 · 디멘젼(계산 단위) · 허용 계산식 · 고정 필터"""
    out = {"name": m["name"], "description": m.get("description", ""), "table": m["table"]}
    if m.get("joins"):
        out["joins"] = m["joins"]
    if m.get("fixed_filters"):
        out["fixed_filters"] = m["fixed_filters"]
    dims = m.get("dimensions") or []
    if dims:
        out["dimensions"] = [by_text(d) for d in dims]
        note = f"집계 계산식은 {', '.join(by_text(d) for d in dims)} 단위로 계산됩니다."
        for d in dims:
            if isinstance(d, dict) and d.get("grain"):
                note += (
                    f" {d['field']} 의 기본 단위는 {d['grain']} 입니다. 다른 단위가 필요하면 group_by 에 "
                    f'{{"field": "{d["field"]}", "grain": …}} 로 적으십시오 ({"·".join(GRAINS)}) '
                    "— 더 큰 단위는 기본 단위 값을 합치고, 더 작은 단위는 다시 계산합니다."
                )
        out["묶음"] = note
    if m.get("formulas"):
        out["formulas"] = list(m["formulas"])
    return out


def _prompt_formula(f: dict, formulas: dict) -> dict:
    expr = expand_formula({"formula": f["name"]}, formulas)
    out = {
        "name": f["name"],
        "level": "집계" if measure_has_aggregate(expr) else "행 단위",
        "mode": f.get("mode", "organic"),
        "식": measure_text(f["expr"]),
    }
    if f.get("description"):
        out["description"] = f["description"]
    if f.get("column_name"):
        out["대신하는 컬럼"] = f["column_name"]
    return out


def system_prompt(
    contract: dict,
    pruned: dict,
    today: datetime.date | None = None,
    chosen_metrics: list | None = None,
) -> str:
    """시스템 프롬프트 생성.

    chosen_metrics: 지표 선택 단계가 고른 지표. 있으면 그 지표를 쓰라고 못박는다 — 지표를 두고 테이블을
    직접 조회하면 지표의 고정 필터·계산식 규칙이 사라진다.
    """
    now = datetime.datetime.now()
    # "최근 5시간" 은 시각이 있어야 계산된다. 날짜를 정해 부른 경우(평가·테스트)에는 시각을 싣지 않는다.
    clock = f" 현재 시각은 {now:%H:%M} 입니다." if today is None or today == now.date() else ""
    today = today or now.date()
    metrics = [_prompt_metric(m) for m in pruned["metrics"]]
    metadata = {"tables": pruned["tables"], "metrics": metrics}
    every = {f["name"]: f for f in contract.get("formulas", [])}
    if pruned.get("formulas"):
        metadata["formulas"] = [_prompt_formula(f, every) for f in pruned["formulas"]]
    if pruned.get("relations"):
        metadata["relations"] = pruned["relations"]

    domain = f"업무 영역: {contract['domain']}\n" if contract.get("domain") else ""
    utc = ""
    if any(c.get("utc_offset") for t in pruned["tables"] for c in t.get("columns", [])):
        utc = (
            "utc_offset 이 붙은 컬럼은 DB 에 UTC 로 저장돼 있지만, 날짜 묶음·필터·표시는 "
            "현지 시각(UTC+utc_offset)으로 자동 보정됩니다. 날짜·시각은 현지 기준 그대로 적고 "
            "직접 바꾸지 마십시오.\n"
        )
    chosen = ""
    if chosen_metrics:
        names = ", ".join(f"'{n}'" for n in chosen_metrics)
        chosen = (
            f"[이번 질문의 지표]\n이전 단계에서 질문에 맞는 지표로 {names} 을(를) 골랐습니다. "
            "ast 의 metric 에 이 지표를 쓰고, 무엇을 볼지(columns·aggregations)와 정렬·기간·필터·묶음을 적으십시오.\n\n"
        )
    formula_guide = (
        FORMULA_GUIDE.format(prefix=MENTION_PREFIX) + "\n\n" if pruned.get("formulas") else ""
    )
    return (
        f"당신은 자연어 질문을 '{contract['name']}' 데이터베이스"
        f"({contract['driver']}) 조회로 바꾸는 시맨틱 파서입니다.\n"
        f"{domain}"
        f"오늘은 {today.isoformat()} ({'월화수목금토일'[today.weekday()]}요일) 입니다.{clock} "
        "“최근 7일”, “지난달”, “올해 7월” 같은 표현은 이 날짜를 기준으로 계산해 "
        "YYYY-MM-DD 형식으로 적으십시오. “최근 5시간” 처럼 시간 단위 기간은 현재 시각을 기준으로 "
        "YYYY-MM-DD HH:MM 형식으로 적으십시오. 기간의 끝은 less_or_equal 로 지정하십시오.\n"
        f"{utc}"
        "아래 [허용된 메타데이터]에 없는 테이블·컬럼·지표 이름은 절대 만들어내지 "
        "마십시오.\n\n"
        f"[허용된 메타데이터]\n{json.dumps(metadata, ensure_ascii=False, indent=2)}\n\n"
        f"{AST_GUIDE}\n\n"
        f"{formula_guide}"
        f"{chosen}"
        "질문을 분석해 compile_sql 도구를 ast 인자로 부르십시오.\n"
        '도구가 "SQL:"로 시작하는 결과를 돌려주면 그것으로 답이 끝난 것입니다.\n'
        '도구가 "error:"로 시작하는 결과를 돌려주면 그 메시지를 읽고 고쳐서 '
        "다시 부르십시오.\n"
        "질문이 여러 갈래로 읽혀 조회를 정할 수 없거나, 지표가 질문에서 받아야 하는 값이 "
        "빠졌다면 추측하지 말고 ask_user 도구로 한 번 되물으십시오. 전체 조회나 합리적인 "
        "기본값으로 답할 수 있으면 되묻지 않습니다."
    )


def run_question(
    contract: dict,
    question: str,
    model,
    top_k: int = 3,
    today: datetime.date | None = None,
    selector: str = "llm",
) -> dict:
    """질문 하나를 처리한다. 상태는 이 호출 안에서만 산다.

    selector: "llm" = 지표 선택 (지표가 테이블·계산식을 정한다) (TO-BE)
              "tfidf" = 테이블·지표 전체를 TF-IDF 로 검색 (기준선)
    돌려주는 것: {sql, error, attempts, ast, clarification, tables, metrics, steps, total_tokens}
    """
    outcome = {
        "sql": None,
        "error": None,
        "attempts": 0,
        "ast": None,
        "clarification": None,
        "tables": [],
        "metrics": [],
        "steps": [],
        "total_tokens": 0,
    }
    # 지표 선택 소스 시작 파이썬 3항연산자 눈에 잘 안들어온다
    try:
        # selection = llm_select(model, contract, question) if selector == "llm" else tfidf_select(contract, question, top_k)
        if selector == "llm":
            selection = llm_select(model, contract, question)
        else:
            selection = tfidf_select(contract, question, top_k)
    except ValueError as error:
        outcome["error"] = str(error)
        return outcome
    outcome.update(
        tables=selection.tables,
        metrics=selection.metrics,
        steps=[s.as_dict() for s in selection.steps],
    )
    # 지표 선택 소스 끝
    if selection.clarification:
        outcome["clarification"] = selection.clarification
        return _with_tokens(outcome)

    record: dict = {}
    chosen = selection.metrics if selector == "llm" else []
    tools = [
        make_compile_tool(
            Compiler(contract), question, record, expected_metrics=chosen
        ),
        make_clarify_tool(question),
    ]
    graph = build_graph(model, tools)

    started = time.monotonic()
    prompt = system_prompt(contract, selection.pruned, today, chosen_metrics=chosen)
    result = graph.invoke(
        {"messages": [SystemMessage(prompt), HumanMessage(question)]},
        {"recursion_limit": MAX_ATTEMPTS * 2 + 3},
    )
    sql_step = Step(
        "sql",
        model=model_name(model),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    for m in result["messages"]:
        if isinstance(m, AIMessage):
            p, c = usage_of(m)
            sql_step.prompt_tokens += p
            sql_step.completion_tokens += c

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    compiles = [
        m for m in tool_messages if not str(m.content).startswith(CLARIFY_PREFIX)
    ]
    outcome["attempts"] = len(compiles)

    success = [m for m in compiles if str(m.content).startswith("SQL:")]
    clarify = [m for m in tool_messages if str(m.content).startswith(CLARIFY_PREFIX)]
    if success:
        outcome["sql"] = str(success[-1].content)[len("SQL:") :].strip()
        outcome["ast"] = record.get("ast")
        sql_step.selected = (
            [json.dumps(outcome["ast"], ensure_ascii=False)] if outcome["ast"] else []
        )
        sql_step.reason = f"{outcome['attempts']}번째 시도에서 컴파일 성공"
    elif clarify:
        outcome["clarification"] = str(clarify[-1].content)[
            len(CLARIFY_PREFIX) :
        ].strip()
        sql_step.reason = "모델이 되물음"
    else:
        outcome["error"] = (
            str(compiles[-1].content)
            if compiles
            else "모델이 조회를 시도하지 않았습니다"
        )
        sql_step.reason = outcome["error"]
    outcome["steps"].append(sql_step.as_dict())
    return _with_tokens(outcome)


def _with_tokens(outcome: dict) -> dict:
    outcome["total_tokens"] = sum(
        s["prompt_tokens"] + s["completion_tokens"] for s in outcome["steps"]
    )
    return outcome
