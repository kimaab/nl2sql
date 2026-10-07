import datetime
import json

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from compiler import Compiler
from graph import MAX_ATTEMPTS, build_graph
from pruner import Pruner
from tool import CLARIFY_PREFIX, make_clarify_tool, make_compile_tool

MAX_EXAMPLES = 6

AST_GUIDE = """[조회 명세(ast) 모양]
ast 는 JSON 객체이고 아래 키만 씁니다. 키 이름을 바꾸지 마십시오 (column 이 아니라 field, table 이 아니라 target_table).
- metric: 지표 이름. 지표의 집계식·조회 컬럼·조인·고정 필터는 자동으로 적용되니 다시 적지 않습니다. metric 을 쓰면 target_table, columns, aggregations, joins 는 적지 않습니다.
  질문이 지표 여러 개를 함께 물으면 이름 목록으로 적습니다 ("metric": ["가동시간", "작업면적"]) — 같은 테이블의 집계형·파생 지표끼리만 됩니다.
- target_table: 맞는 지표가 없을 때 조회할 테이블.
- columns: 행 목록을 볼 때의 컬럼 ["컬럼", "테이블.컬럼"]. aggregations 와 함께 쓰지 않습니다.
- aggregations: [{"field": "컬럼 또는 *", "function": "COUNT", "alias": "선택"}]. function 은 SUM, COUNT, AVG, MIN, MAX, COUNT_DISTINCT 중 하나이고 괄호 없이 이름만 적습니다 ("COUNT(*)"가 아니라 function 은 "COUNT", field 는 "*").
- joins: target_table 에 이어 붙일 테이블 이름 목록 ["테이블"]. 메타데이터의 relations 에 있는 관계만 쓸 수 있고 ON 조건은 자동입니다. 다른 테이블의 컬럼은 "테이블.컬럼" 으로 적습니다.
- filters: [{"field": "컬럼", "operator": "equals", "value": "값"}] — 모두 AND 로 묶입니다.
  operator: equals, not_equals, greater_than, greater_or_equal, less_than, less_or_equal,
  in, not_in (value 는 목록), is_null, is_not_null (value 없음), contains, starts_with, ends_with (문자열 일부 일치).
  OR 는 {"any_of": [조건, 조건]} 으로, 그 안의 AND 묶음은 {"all_of": [조건, 조건]} 으로 적습니다.
  컬럼에 codes(코드 사전)가 있으면 value 에 코드나 그 이름(label)을 적습니다.
- group_by: ["컬럼"]. 날짜를 기간 단위로 묶을 때는 {"field": "날짜컬럼", "grain": "day|week|month|quarter|year"} — 결과 별칭은 "컬럼_grain" (예: ORDER_DTM_month).
- having: 집계 결과 조건 [{"alias": "집계 별칭 또는 지표 이름", "operator": "greater_or_equal", "value": 3}].
- order_by: [{"field": "컬럼 또는 집계 별칭", "direction": "asc|desc"}]. 집계 질의는 group_by 컬럼이나 집계 별칭으로만 정렬합니다.
- limit: 1~1000 정수. "상위 10개", "최근 5건" 은 order_by 와 limit 을 함께 씁니다.
- distinct: true — 행 목록의 중복을 뺄 때. 집계의 중복 제거는 COUNT_DISTINCT 입니다.
- compare: 기간 비교 {"field": "날짜컬럼", "periods": [{"label": "이번달", "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}, {"label": "지난달", "from": "...", "to": "..."}]}.
  첫 기간이 기준이고, 기간이 둘이면 증감률이 함께 나옵니다. 집계(aggregations 나 집계형 지표)에만 씁니다."""


def system_prompt(contract: dict, pruned: dict, today: datetime.date | None = None) -> str:
    """시스템 프롬프트 생성. 지표 예시는 메타데이터와 따로 모범 답안으로 싣는다."""
    today = today or datetime.date.today()
    metrics = [{k: v for k, v in m.items() if k != "examples"} for m in pruned["metrics"]]
    metadata = {"tables": pruned["tables"], "metrics": metrics}
    if pruned.get("relations"):
        metadata["relations"] = pruned["relations"]

    examples = [
        e for m in pruned["metrics"] for e in m.get("examples", []) if e.get("ast")
    ][:MAX_EXAMPLES]
    example_text = ""
    if examples:
        example_text = "[예시]\n" + "\n".join(
            f"질문: {e['question']}\nast: {json.dumps(e['ast'], ensure_ascii=False)}" for e in examples
        ) + "\n\n"

    return (
        f"당신은 자연어 질문을 '{contract['name']}' 데이터베이스"
        f"({contract['driver']}) 조회로 바꾸는 시맨틱 파서입니다.\n"
        f"오늘은 {today.isoformat()} ({'월화수목금토일'[today.weekday()]}요일) 입니다. "
        "“최근 7일”, “지난달”, “올해 7월” 같은 표현은 이 날짜를 기준으로 계산해 "
        "YYYY-MM-DD 형식으로 적으십시오. 기간의 끝은 less_or_equal 로 지정하십시오.\n"
        "아래 [허용된 메타데이터]에 없는 테이블·컬럼·지표 이름은 절대 만들어내지 "
        "마십시오.\n\n"
        f"[허용된 메타데이터]\n{json.dumps(metadata, ensure_ascii=False, indent=2)}\n\n"
        f"{AST_GUIDE}\n\n"
        f"{example_text}"
        "질문을 분석해 compile_sql 도구를 ast 인자로 부르십시오.\n"
        "도구가 \"SQL:\"로 시작하는 결과를 돌려주면 그것으로 답이 끝난 것입니다.\n"
        "도구가 \"error:\"로 시작하는 결과를 돌려주면 그 메시지를 읽고 고쳐서 "
        "다시 부르십시오.\n"
        "질문이 여러 갈래로 읽혀 조회를 정할 수 없거나, 지표가 질문에서 받아야 하는 값이 "
        "빠졌다면 추측하지 말고 ask_user 도구로 한 번 되물으십시오. 전체 조회나 합리적인 "
        "기본값으로 답할 수 있으면 되묻지 않습니다."
    )


def run_question(contract: dict, question: str, model, top_k: int = 3,
                 today: datetime.date | None = None, semantic: dict[str, float] | None = None,
                 semantic_min: float = 0.0) -> dict:
    """질문 하나를 처리한다. 상태는 이 호출 안에서만 산다.

    semantic: 의미 검색이 낸 {지표 이름: 유사도}, semantic_min 미만은 후보에서 뺀다.
    없으면 TF-IDF 만으로 후보를 고른다.
    돌려주는 것: {sql, error, attempts, ast, clarification}
    """
    pruned = Pruner(contract).prune(question, top_k=top_k, semantic=semantic, semantic_min=semantic_min)
    record: dict = {}
    tools = [make_compile_tool(Compiler(contract), question, record), make_clarify_tool(question)]
    graph = build_graph(model, tools)

    result = graph.invoke(
        {"messages": [SystemMessage(system_prompt(contract, pruned, today)), HumanMessage(question)]},
        {"recursion_limit": MAX_ATTEMPTS * 2 + 3},
    )

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    compiles = [m for m in tool_messages if not str(m.content).startswith(CLARIFY_PREFIX)]
    attempts = len(compiles)
    outcome = {"sql": None, "error": None, "attempts": attempts, "ast": None, "clarification": None}

    success = [m for m in compiles if str(m.content).startswith("SQL:")]
    if success:
        outcome["sql"] = str(success[-1].content)[len("SQL:"):].strip()
        outcome["ast"] = record.get("ast")
        return outcome

    clarify = [m for m in tool_messages if str(m.content).startswith(CLARIFY_PREFIX)]
    if clarify:
        outcome["clarification"] = str(clarify[-1].content)[len(CLARIFY_PREFIX):].strip()
        return outcome

    outcome["error"] = str(compiles[-1].content) if compiles else "모델이 조회를 시도하지 않았습니다"
    return outcome
