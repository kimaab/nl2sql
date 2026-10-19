"""질문 하나를 처리한다: 테이블 선택(지표 필요 판단) → AST 생성(LLM) → 컴파일(조합·검증).

지표가 필요한 질문인데 맞는 지표가 없으면 SQL 을 만들지 않고 '지표 등록이 필요합니다' 로 끝난다.
지표를 쓰면 고른 지표(질문이 묻는 계산마다 하나)의 AST 를 모두 LLM 에 주고,
질문에 맞게 조건만 바꾸거나 지표들의 계산 식을 한 AST 로 합치게 한다.
"""

import datetime
import json

from langchain_core.messages import HumanMessage, SystemMessage

from compiler import Compiler, temporal_kind
from selector import Step, chat, model_name, parse_json, select_tables

MAX_ATTEMPTS = 3

AST_SYSTEM = """당신은 질문을 '{name}' 데이터베이스({driver})의 조회 명세(AST, JSON)로 바꾸는 역할입니다.
{domain}[오늘]
{today}. "최근 7일", "지난달" 같은 표현은 이 날짜를 기준으로 계산해 'YYYY-MM-DD' 또는 'YYYY-MM-DD HH:MM:SS' 로 적습니다.

[AST 모양]
{{
  "from": "테이블" 또는 {{"query": 하위 AST, "as": "별칭"}},
  "joins": [{{"table": "테이블", "type": "inner|left", "on": [["a.컬럼", "b.컬럼"]]}}],   (필요할 때만)
  "select": [{{"expr": 식, "as": "결과 이름"}}],   모든 컬럼은 {{"expr": {{"col": "*"}}}}
  "distinct": true,   (중복을 뺄 때만)
  "where": [조건], "group_by": [식], "having": [조건],
  "order_by": [{{"expr": 식, "dir": "asc|desc"}}], "limit": 숫자
}}
식: {{"col": "컬럼"}} | {{"num": 1}} | {{"str": "문자"}} | {{"fn": "함수", "args": [식, ...]}}
  집계: SUM AVG MIN MAX COUNT(인자 없으면 *) COUNT_DISTINCT
  계산: ADD SUB MUL DIV(0 으로 나누면 NULL)
  함수: ABS ROUND COALESCE NULLIF GREATEST LEAST
  날짜: DATE(날짜만), {{"fn": "DATE_TRUNC", "unit": "hour|day|week|month|year", "args": [식]}},
        ADD_MINUTES · ADD_HOURS · ADD_DAYS [식, {{"num": n}}], NOW, CURRENT_DATE
  순위: {{"fn": "ROW_NUMBER", "partition_by": [식], "order_by": [{{"expr": 식, "dir": "desc"}}]}}
  분기: {{"fn": "CASE", "when": [{{"if": [조건], "then": 식}}], "else": 식}}
조건: {{"expr": 식, "op": "= != > >= < <= in not_in between like not_like is_null is_not_null", "value": 값 또는 식}}
      목록은 모두 AND. OR 은 {{"or": [조건, ...]}}, 그 안의 AND 묶음은 {{"and": [조건, ...]}}

[규칙]
- 아래 [테이블] 에 없는 테이블·컬럼은 쓰지 않습니다. 여러 테이블을 쓰면 컬럼을 '테이블.컬럼' 으로 적습니다.
- 집계 안에 집계는 쓸 수 없습니다. 두 단계 집계(예: 장비·일별로 먼저 구한 뒤 일별 합계)는 from 에 하위 쿼리를 둡니다.
- 하위 쿼리 바깥에서는 하위 쿼리 select 의 "as" 이름을 컬럼으로 씁니다.
- 순위로 거르려면(예: 단위별 마지막 건) 하위 쿼리에서 ROW_NUMBER 를 구하고 바깥 where 에서 거릅니다.
- 집계하지 않은 select 항목은 group_by 에 같은 식으로 넣습니다.
- 컬럼 뒤의 [UTC 보정] 은 DB 에 UTC 로 저장됐지만 자동으로 현지 시각으로 바뀌는 컬럼입니다. 날짜·시각은 현지 기준 그대로 적고, ADD_HOURS 로 직접 바꾸지 마십시오.
- 시각을 포함한 컬럼의 기간은 시작은 >=, 끝은 < 다음 날로 적습니다.
- 질문만으로 조회를 정할 수 없으면 {{"clarify": "사용자에게 물을 한 문장"}} 으로 답합니다.
- 설명 없이 JSON 객체 하나로만 답합니다: {{"ast": AST, "reason": "설명"}}"""

METRIC_GUIDE = """[지표] {name} — {description}
등록된 SQL:
{sql}
등록된 AST:
{ast}
이 지표의 AST 를 바탕으로 질문에 맞게 기간·조건·묶음·정렬·개수만 바꾸십시오. 계산 부분(select 의 식)은 그대로 둡니다."""

METRICS_RULE = """[지표 여러 개]
위 지표들의 계산 식을 한 AST 에 합칩니다.
- 묶음(group_by)과 기간이 같으면 각 지표의 select 계산 식을 그대로 옮겨 나란히 둡니다.
- 질문이 지표끼리의 계산(차이, 합, 비율)을 물으면 각 지표의 계산 식을 그대로 SUB, ADD, DIV 의 인자로 씁니다.
- 지표에 없는 새 계산([계산 필요] 컬럼의 집계)은 만들지 않습니다. 결과 이름("as")은 그 지표가 뜻하는 값으로 붙입니다."""


def column_line(c: dict, driver: str) -> str:
    note = " ".join(str(c.get("description") or "").split())
    if c.get("utc_offset") and temporal_kind(c.get("type", ""), driver) == "datetime":
        note = f"{note} [UTC 보정]".strip()
    return f"  - {c['name']} ({c.get('type', '')}){': ' + note if note else ''}"


def ast_prompt(contract: dict, tables: list, metrics: list, now: datetime.datetime) -> tuple[str, str]:
    """→ (시스템 프롬프트, 사용자 메시지 앞부분)"""
    driver = contract.get("driver") or "postgresql"
    today = f"{now:%Y-%m-%d} ({'월화수목금토일'[now.weekday()]}요일) 현재 시각 {now:%H:%M}"
    domain = f"[업무 영역]\n{contract['domain']}\n" if contract.get("domain") else ""
    system = AST_SYSTEM.format(name=contract.get("name", ""), driver=driver, domain=domain, today=today)
    blocks = []
    for t in contract["tables"]:
        if t["name"] in tables:
            note = t.get("purpose") or t.get("description") or ""
            lines = []
            for c in t["columns"]:
                lines.append(column_line(c, driver))
            blocks.append(f"[{t['name']}] {' '.join(note.split())}\n" + "\n".join(lines))
    for r in contract.get("relations") or []:
        if r["left_table"] in tables and r["right_table"] in tables:
            blocks.append(f"[관계] {r['left_table']}.{r['left_column']} = {r['right_table']}.{r['right_column']}")
    user = "[테이블]\n" + "\n\n".join(blocks)
    for metric in metrics:
        user += "\n\n" + METRIC_GUIDE.format(
            name=metric["name"],
            description=" ".join(str(metric.get("description") or "").split()) or "(설명 없음)",
            sql=metric["sql"],
            ast=json.dumps(metric["ast"], ensure_ascii=False),
        )
    if len(metrics) > 1:
        user += "\n\n" + METRICS_RULE
    return system, user


def run_question(contract: dict, question: str, model, now: datetime.datetime | None = None) -> dict:
    """질문 하나를 처리한다. 상태는 이 호출 안에서만 산다.

    돌려주는 것: {sql, error, attempts, ast, clarification, needs_metric, tables, metrics, steps, total_tokens}
    """
    outcome = {
        "sql": None,
        "error": None,
        "attempts": 0,
        "ast": None,
        "clarification": None,
        "needs_metric": False,
        "tables": [],
        "metrics": [],
        "steps": [],
        "total_tokens": 0,
    }
    try:
        selection = select_tables(model, contract, question)
    except ValueError as error:
        outcome["error"] = str(error)
        return outcome
    outcome["tables"] = selection.tables
    outcome["metrics"] = list(selection.metrics)
    outcome["steps"].append(selection.step.as_dict())
    if selection.clarification:
        outcome["clarification"] = selection.clarification
        return _with_tokens(outcome)
    if selection.needs_metric and not selection.metrics:
        outcome["needs_metric"] = True
        outcome["error"] = f"지표 등록이 필요합니다 — {selection.reason or '계산이 필요한 질문인데 맞는 지표가 없습니다'}"
        return _with_tokens(outcome)

    metrics = []
    for name in selection.metrics:
        for m in contract.get("metrics") or []:
            if m["name"] == name:
                metrics.append(m)
                break
    system, user = ast_prompt(contract, selection.tables, metrics, now or datetime.datetime.now())
    messages = [SystemMessage(system), HumanMessage(f"{user}\n\n[질문]\n{question}")]
    compiler = Compiler(contract)
    step = Step("sql", model=model_name(model))
    for attempt in range(1, MAX_ATTEMPTS + 1):
        reply = chat(model, messages, step, attempt)
        outcome["attempts"] = attempt
        try:
            parsed = parse_json(reply.content)
            if str(parsed.get("clarify") or "").strip() and not parsed.get("ast"):
                outcome.update(clarification=str(parsed["clarify"]).strip(), error=None)
                step.reason = "모델이 되물음"
                break
            sql = compiler.compile(parsed.get("ast"))
        except (ValueError, KeyError, TypeError) as error:
            outcome["error"] = str(error)
            step.reason = f"{attempt}번째 시도 실패: {error}"
            messages += [reply, HumanMessage(f"오류: {error}\n고쳐서 JSON 으로 다시 답하십시오.")]
            continue
        outcome.update(sql=sql, ast=parsed["ast"], error=None)
        step.selected = [json.dumps(parsed["ast"], ensure_ascii=False)]
        step.reason = str(parsed.get("reason") or f"{attempt}번째 시도에서 컴파일 성공")
        break
    outcome["steps"].append(step.as_dict())
    return _with_tokens(outcome)


def _with_tokens(outcome: dict) -> dict:
    total = 0
    for s in outcome["steps"]:
        total += s["prompt_tokens"] + s["completion_tokens"]
    outcome["total_tokens"] = total
    return outcome
