"""질문 하나를 처리한다: 질문 분석(조회 항목, 테이블, 묶음, 조건, 지표 필요 판단) → AST 생성(LLM) → 컴파일(조합·검증).

질문 분석에서 정할 수 없는 항목이 있으면 사용자에게 묻고, 조회 항목이 '전체' 면 그 테이블의 컬럼 목록을 보여 주고 멈춘다.
사용자는 목록에서 필요한 컬럼을 골라 질문에 붙여 다시 묻는다. 분석 결과는 AST 단계에 그대로 넘긴다.

지표가 필요한 질문인데 맞는 지표가 없으면 SQL 을 만들지 않고 '지표 등록이 필요합니다' 로 끝난다.
지표를 쓰면 고른 지표(질문이 묻는 계산마다 하나)의 AST 를 모두 LLM 에 주고,
질문에 맞게 조건만 바꾸거나 지표들의 계산 식을 한 AST 로 합치게 한다.
"""

import copy
import datetime
import json
import re

from langchain_core.messages import HumanMessage, SystemMessage

from compiler import CalcColumnError, Compiler, temporal_kind
from selector import TRUNCATED, Step, chat, follow_up, model_name, parse_json, select_tables, truncated

MAX_ATTEMPTS = 5

AST_SYSTEM = """당신은 질문을 '{name}' 데이터베이스({driver})의 조회 명세(AST, JSON)로 바꾸는 역할입니다.
{domain}[오늘]
{today}. "오늘", "어제", "최근 7일", "지난달" 같은 표현은 이 날짜를 기준으로 계산해 'YYYY-MM-DD' 또는 'YYYY-MM-DD HH:MM:SS' 로 적습니다.
기간 조건에 CURRENT_DATE 를 쓰지 않습니다.

[AST 모양]
{{
  "with": [{{"name": "이름", "query": 하위 AST}}],   (같은 하위 쿼리를 두 곳 이상에서 쓸 때만. 맨 바깥에만 둡니다)
  "from": "테이블" 또는 {{"table": "테이블", "as": "별칭"}} 또는 {{"query": 하위 AST, "as": "별칭"}} 또는 {{"tree": 계층, "as": "별칭"}},
  "joins": [{{"table": "테이블" 또는 "query": 하위 AST, "as": "별칭", "type": "inner|left", "on": [["a.컬럼", "b.컬럼"]]}}],   (필요할 때만)
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
        ADD_SECONDS · ADD_MINUTES · ADD_HOURS · ADD_DAYS [식, {{"num": n}}], NOW, CURRENT_DATE
  순위: {{"fn": "ROW_NUMBER", "partition_by": [식], "order_by": [{{"expr": 식, "dir": "desc"}}]}}
  분기: {{"fn": "CASE", "when": [{{"if": [조건], "then": 식}}], "else": 식}}
조건: {{"expr": 식, "op": "= != > >= < <= in not_in between like not_like is_null is_not_null", "value": 값 또는 식}}
  in · not_in 의 value 는 {{"query": 하위 AST}} (select 하나) 도 됩니다
계층: {{"table": "테이블", "key": "id 컬럼", "parent": "부모 id 컬럼", "start": [조건], "direction": "down|up"}}
  start 조건에 맞는 행에서 시작해 자식 쪽(down) 또는 부모 쪽(up)으로 따라간 행 전부입니다. 테이블의 컬럼에 depth(시작 = 0)가 더해집니다.
      목록은 모두 AND. OR 은 {{"or": [조건, ...]}}, 그 안의 AND 묶음은 {{"and": [조건, ...]}}

[규칙]
- 아래 [테이블] 에 없는 테이블·컬럼은 쓰지 않습니다. 여러 테이블을 쓰면 컬럼을 '테이블.컬럼' 으로 적습니다.
- 집계 안에 집계는 쓸 수 없습니다. 두 단계 집계(예: 장비·일별로 먼저 구한 뒤 일별 합계)는 from 에 하위 쿼리를 둡니다.
- 상위·하위 관계(그룹 계층, 조직도, 메뉴 트리)를 따라가야 하면 tree 를 씁니다. 시작이 다른 테이블의 값이면
  (예: 사용자의 그룹) start 에 {{"expr": {{"col": "id"}}, "op": "in", "value": {{"query": 하위 AST}}}} 로 적습니다.
  타입이 다른 컬럼끼리(정수 id 와 문자 parent_id) 비교해도 컴파일러가 맞춰 주므로 변환하지 않습니다.
- 하위 쿼리 바깥에서는 하위 쿼리 select 의 "as" 이름을 컬럼으로 씁니다.
- 순위로 거르려면(예: 단위별 마지막 건) 하위 쿼리에서 ROW_NUMBER 를 구하고 바깥 where 에서 거릅니다.
- with 에 정의한 이름은 from · joins 에서 테이블처럼 씁니다 ("from": "daily"). 컬럼은 그 하위 쿼리 select 의 "as" 이름입니다.
- 기간 합계로 고른 상위 N개 묶음의 상세(예: 50일 합계 상위 5대 차량의 일별 값)는, 상세 집계(예: 차량·일별)를 with 에
  한 번 정의하고, from 은 그 이름, joins 에는 그 이름을 묶음별로 합쳐 상위 N개를 고르는 하위 쿼리(묶음별 합계, order_by, limit)를
  {{"query": …, "as": "top", "type": "inner"}} 로 두어 묶음 컬럼으로 조인합니다. 원본 테이블을 두 번 읽지 않습니다.
  상세 쪽을 order_by 와 limit 으로 자르면 묶음이 아니라 행이 잘립니다.
- 집계하지 않은 select 항목은 group_by 에 같은 식으로 넣습니다.
- 컬럼 뒤의 [UTC 보정] 은 DB 에 UTC 로 저장됐지만 자동으로 현지 시각으로 바뀌는 컬럼입니다. 날짜·시각은 현지 기준 그대로 적고, ADD_HOURS 로 직접 바꾸지 마십시오.
  '행마다 … 시차' 가 붙은 컬럼은 그 행의 시차 컬럼으로 보정됩니다 (예: 차량별 타임존). 시차를 직접 더하지 않습니다.
  최근 수신 여부(예: 10초 이내)는 이 컬럼을 NOW 기준 값(ADD_SECONDS(NOW, -10))과 비교하면 됩니다. 시간대는 컴파일러가 맞춥니다.
- 시각을 포함한 컬럼의 기간은 시작은 >=, 끝은 < 다음 날로 적습니다.
- [질문 분석] 은 앞 단계에서 질문을 나눈 결과입니다. select 는 조회 항목을, group_by 는 묶음 항목을, where 는 조건을 따릅니다.
  조회 항목을 더하거나 빼지 않습니다. 결과 이름("as")은 조회 항목의 이름을 씁니다.
- 질문만으로 조회를 정할 수 없으면 {{"clarify": "사용자에게 물을 한 문장"}} 으로 답합니다.
- 설명 없이 JSON 객체 하나로만 답합니다: {{"ast": AST, "reason": "설명"}}"""

METRIC_GUIDE = """[지표] {name} — {description}
등록된 SQL:
{sql}
등록된 AST:
{ast}
이 지표의 AST 를 바탕으로 질문에 맞게 기간·조건·묶음·정렬·개수만 바꾸십시오. 계산 부분(select 의 식)은 그대로 둡니다."""

# 지표 하나를 그대로 쓰거나 값만 바꾸는 질문에서 LLM 이 긴 AST 를 베껴 쓰다 틀리는 일을 막는다
AS_IS_RULE = """[그대로 쓰기]
- 질문에 맞게 바꿀 것이 하나도 없어 등록된 AST 가 그대로 답이면, AST 를 다시 적지 말고 {"as_is": true, "reason": "설명"} 으로만 답합니다.
- 조건의 값만 바뀌면 (예: 아이디, 임계치, 코드값) AST 를 다시 적지 말고
  {"replace": [{"from": "등록된 AST 의 값", "to": "질문의 값"}], "reason": "설명"} 으로만 답합니다.
  from 은 등록된 AST 에 있는 값 그대로입니다. 같은 값은 모든 곳에서 바뀝니다.
- 기간·묶음·정렬·개수처럼 값 말고 다른 것이 바뀌면 AST 를 적습니다."""

METRICS_RULE = """[지표 여러 개]
위 지표들의 계산 식을 한 AST 에 합칩니다.
- 묶음(group_by)과 기간이 같으면 각 지표의 select 계산 식을 그대로 옮겨 나란히 둡니다.
- 질문이 지표끼리의 계산(차이, 합, 비율)을 물으면 각 지표의 계산 식을 그대로 SUB, ADD, DIV 의 인자로 씁니다.
- 지표에 없는 새 계산([계산 필요] 컬럼의 집계)은 만들지 않습니다. 결과 이름("as")은 그 지표가 뜻하는 값으로 붙입니다."""


def column_line(c: dict, driver: str) -> str:
    note = " ".join(str(c.get("description") or "").split())
    if c.get("utc_offset") and temporal_kind(c.get("type", ""), driver) == "datetime":
        if c.get("utc_offset_column"):
            note = f"{note} [UTC 보정: 행마다 {c['utc_offset_column']} 시차, 비면 {c['utc_offset']:+d}시간]".strip()
        else:
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
    elif metrics:
        user += "\n\n" + AS_IS_RULE
    return system, user


def column_label(c: dict) -> str:
    """컬럼의 한글 이름: 설명의 앞부분 (코드값 풀이와 괄호는 뺀다). 설명이 없으면 컬럼 이름"""
    text = " ".join(str(c.get("description") or "").split())
    text = re.split(r"[:：(（\[]", text, maxsplit=1)[0].strip()
    return text or c["name"]


def column_choices(contract: dict, tables: list) -> list:
    """조회 항목이 '전체' 인 테이블의 컬럼 목록 → [{table, description, columns: [{name, label}], text}]
    text 는 사용자가 필요한 것만 남겨 질문에 붙여 넣는 한 줄이다."""
    out = []
    for t in contract["tables"]:
        if t["name"] not in tables:
            continue
        columns = []
        parts = []
        for c in t["columns"]:
            label = column_label(c)
            columns.append({"name": c["name"], "label": label})
            parts.append(label if label == c["name"] else f"{label}({c['name']})")
        note = " ".join(str(t.get("purpose") or t.get("description") or "").split())
        out.append(
            {
                "table": t["name"],
                "description": note,
                "columns": columns,
                "text": f"조회 항목({t['name']}): " + ", ".join(parts),
            }
        )
    return out


def star_tables(analysis: dict, tables: list) -> list:
    """조회 항목이 '전체' 인 테이블. 테이블 없이 '*' 만 적었으면 고른 테이블 전부"""
    out = []
    for item in analysis.get("items") or []:
        if item["column"] == "*":
            for t in tables:
                if t not in out:
                    out.append(t)
        elif item["column"].endswith(".*") and item["column"][:-2] not in out:
            out.append(item["column"][:-2])
    return out


def analysis_block(analysis: dict) -> str:
    """AST 단계에 넘기는 질문 분석. 비었으면 빈 문자열"""

    def line(part: dict) -> str:
        text = part["name"]
        if part.get("column") and part["column"] != part["name"]:
            text += f" ({part['column']})"
        if part.get("value"):
            text += f" = {part['value']}"
        return text

    if not analysis or not (analysis.get("items") or analysis.get("conditions") or analysis.get("dimensions")):
        return ""
    lines = ["[질문 분석]"]
    items = []
    for p in analysis.get("items") or []:
        items.append(line(p))
    lines.append("- 조회 항목: " + (", ".join(items) if items else "(질문에서 정하지 않음)"))
    if analysis.get("grouping"):
        dims = []
        for p in analysis.get("dimensions") or []:
            dims.append(line(p))
        lines.append("- 묶음: 있음, 묶음 항목: " + (", ".join(dims) if dims else "(질문에서 정하지 않음)"))
    else:
        lines.append("- 묶음: 없음 (행을 그대로 보여 줍니다)")
    conditions = []
    for p in analysis.get("conditions") or []:
        conditions.append(line(p))
    lines.append("- 조건: " + (", ".join(conditions) if conditions else "없음"))
    return "\n".join(lines)


def with_user(question: str, user_id: str) -> str:
    """질문에 질문한 사용자를 붙인다. '나', '내 권한', '로그인한 사용자' 를 LLM 이 이 아이디로 풀게 한다."""
    user_id = " ".join(str(user_id or "").split())
    if not user_id:
        return question
    return (
        f"[현재 사용자]\n로그인한 사용자 아이디: {user_id}\n"
        f"질문의 '나', '내', '현재 로그인한 사용자' 는 이 사용자입니다. 조건에 이 아이디를 그대로 씁니다.\n\n"
        f"[질문]\n{question}"
    )


def run_question(contract: dict, question: str, model, now: datetime.datetime | None = None, user_id: str = "") -> dict:
    """질문 하나를 처리한다. 상태는 이 호출 안에서만 산다.

    돌려주는 것: {sql, error, attempts, ast, clarification, columns, analysis, needs_metric, missing, tables, metrics,
    steps, total_tokens}
    """
    outcome = {
        "sql": None,
        "error": None,
        "attempts": 0,
        "ast": None,
        "clarification": None,
        "columns": [],  # 조회 항목이 '전체' 일 때 고를 컬럼 목록 (column_choices)
        "analysis": {},  # 질문 분석 (selector.Selection.analysis)
        "needs_metric": False,
        "missing": [],
        "tables": [],
        "metrics": [],
        "steps": [],
        "total_tokens": 0,
    }
    question = with_user(question, user_id)
    try:
        selection = select_tables(model, contract, question)
    except ValueError as error:
        outcome["error"] = str(error)
        return outcome
    outcome["tables"] = selection.tables
    outcome["metrics"] = list(selection.metrics)
    outcome["steps"].append(selection.step.as_dict())
    outcome["analysis"] = selection.analysis
    # 조회 항목이 '전체' 면 컬럼 목록을 보여 주고 멈춘다 — 사용자가 필요한 컬럼만 골라 다시 묻는다
    stars = star_tables(selection.analysis, selection.tables)
    if stars:
        outcome["columns"] = column_choices(contract, stars)
    if selection.clarification or outcome["columns"]:
        outcome["clarification"] = selection.clarification or (
            "조회할 컬럼을 정해 주십시오. 아래 목록에서 필요한 컬럼만 남겨 질문에 붙여 다시 보내세요."
        )
        return _with_tokens(outcome)

    metrics = []
    for name in selection.metrics:
        for m in contract.get("metrics") or []:
            if m["name"] == name:
                metrics.append(m)
                break
    # 지표를 골랐어도 그 지표들이 하지 않는 계산이 남았으면 지표 등록이 먼저다
    missing = []
    for m in selection.missing:
        if m["column"] and _covered(metrics, m["column"]):
            continue
        if not m["column"] and m["name"] in selection.metrics:
            continue
        missing.append(m)
    if selection.needs_metric and (not metrics or missing):
        outcome["needs_metric"] = True
        outcome["missing"] = missing
        outcome["error"] = f"지표 등록이 필요합니다 — {selection.reason or '계산이 필요한 질문인데 맞는 지표가 없습니다'}"
        return _with_tokens(outcome)

    system, user = ast_prompt(contract, selection.tables, metrics, now or datetime.datetime.now())
    analysis = analysis_block(selection.analysis)
    if analysis:
        user += "\n\n" + analysis
    messages = [SystemMessage(system), HumanMessage(f"{user}\n\n[질문]\n{question}")]
    # [계산 필요] 컬럼의 계산은 고른 지표의 식으로만 허용한다 — 지표 없이 LLM 이 만든 계산은 틀리기 쉽다
    compiler = Compiler(contract, metrics)
    step = Step("sql", model=model_name(model))
    for attempt in range(1, MAX_ATTEMPTS + 1):
        reply = chat(model, messages, step, attempt)
        outcome["attempts"] = attempt
        try:
            if truncated(reply):
                raise ValueError(TRUNCATED)
            parsed = parse_json(reply.content)
            if str(parsed.get("clarify") or "").strip() and not parsed.get("ast"):
                outcome.update(clarification=str(parsed["clarify"]).strip(), error=None)
                step.reason = "모델이 되물음"
                break
            ast = parsed.get("ast")
            if not ast and (parsed.get("as_is") is True or parsed.get("replace")):
                if len(metrics) != 1:
                    raise ValueError("as_is · replace 는 지표 하나를 쓸 때만 답합니다. AST 를 적으십시오")
                # 등록된 AST 를 그대로 쓰거나 값만 바꿔 쓴다 — 지표 원본은 건드리지 않는다
                ast = copy.deepcopy(metrics[0]["ast"])
                if parsed.get("replace"):
                    _apply_replace(ast, parsed["replace"])
            sql = compiler.compile(ast)
        except CalcColumnError as error:
            if not _covered(metrics, error.column):
                # 고른 지표 어디에도 이 컬럼의 계산이 없다 — 다시 시켜도 지표 없이 계산할 뿐이라 지표 등록으로 넘긴다
                _needs_metric_for(outcome, contract, error)
                step.reason = f"{attempt}번째 시도: {error}"
                break
            outcome["error"] = str(error)
            step.reason = f"{attempt}번째 시도 실패: {error}"
            messages += follow_up(reply, f"오류: {error}\n고쳐서 JSON 으로 다시 답하십시오.")
            continue
        except (ValueError, KeyError, TypeError) as error:
            outcome["error"] = str(error)
            step.reason = f"{attempt}번째 시도 실패: {error}"
            messages += follow_up(reply, f"오류: {error}\n고쳐서 JSON 으로 다시 답하십시오.")
            continue
        outcome.update(sql=sql, ast=ast, error=None)
        step.selected = [json.dumps(ast, ensure_ascii=False)]
        step.reason = str(parsed.get("reason") or f"{attempt}번째 시도에서 컴파일 성공")
        break
    outcome["steps"].append(step.as_dict())
    return _with_tokens(outcome)


def _apply_replace(ast: dict, pairs) -> None:
    """replace 의 [{from, to}] 를 AST 의 조건 값에 적용한다. 등록된 AST 에 없는 값이면 ValueError"""
    if not isinstance(pairs, list):
        raise ValueError('replace 는 [{"from": 값, "to": 값}] 모양입니다')
    for pair in pairs:
        if not isinstance(pair, dict) or "from" not in pair or "to" not in pair:
            raise ValueError('replace 는 [{"from": 값, "to": 값}] 모양입니다')
        if _replace_values(ast, pair["from"], pair["to"]) == 0:
            raise ValueError(f"replace 의 from 값 {pair['from']!r} 이 등록된 AST 에 없습니다. AST 에 있는 값 그대로 적으십시오")


def _replace_values(node, old, new) -> int:
    """조건 값(value · str · num) 중 old 와 같은 것을 new 로 바꾼다. 컬럼 · 테이블 이름은 건드리지 않는다. 바꾼 수를 돌려준다"""
    count = 0
    if isinstance(node, list):
        for x in node:
            count += _replace_values(x, old, new)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in ("value", "str", "num") and _same_value(value, old):
                node[key] = new
                count += 1
            elif key == "value" and isinstance(value, list):  # in · between 의 값 목록
                for i, x in enumerate(value):
                    if _same_value(x, old):
                        value[i] = new
                        count += 1
                    else:
                        count += _replace_values(x, old, new)
            else:
                count += _replace_values(value, old, new)
    return count


def _same_value(a, b) -> bool:
    """값 하나끼리 같은가. LLM 이 숫자를 문자로 적어도("90") 같은 값으로 본다"""
    for x in (a, b):
        if isinstance(x, (dict, list, bool)) or x is None:
            return False
    return str(a) == str(b)


def _covered(metrics: list, column: str) -> bool:
    """고른 지표 중 이 컬럼을 쓰는 것이 있나"""
    for m in metrics:
        if column in _columns_in(m.get("ast")):
            return True
    return False


def _columns_in(node, out: set | None = None) -> set:
    """AST 가 쓰는 컬럼 이름 ('테이블.' 은 뺀다). 계층의 key · parent 도 센다."""
    out = set() if out is None else out
    if isinstance(node, list):
        for x in node:
            _columns_in(x, out)
    elif isinstance(node, dict):
        if isinstance(node.get("col"), str):
            out.add(node["col"].split(".")[-1])
        if isinstance(node.get("tree"), dict):
            for key in ("key", "parent"):
                if isinstance(node["tree"].get(key), str):
                    out.add(node["tree"][key])
        for value in node.values():
            _columns_in(value, out)
    return out


def _needs_metric_for(outcome: dict, contract: dict, error) -> None:
    """지표 없는 [계산 필요] 컬럼 계산 → '지표 등록이 필요합니다' 로 끝낸다 (테이블 선택 단계가 놓친 경우)"""
    name = error.column
    for t in contract["tables"]:
        if t["name"] == error.table:
            for c in t["columns"]:
                if c["name"] == error.column and str(c.get("description") or "").strip():
                    name = " ".join(str(c["description"]).split())
    outcome["needs_metric"] = True
    outcome["missing"] = [{"name": name, "table": error.table, "column": error.column}]
    outcome["error"] = (
        f"지표 등록이 필요합니다 — {error.table}.{error.column}({name}) 은 계산이 필요한 컬럼인데 이 계산의 지표가 없습니다"
    )


def _with_tokens(outcome: dict) -> dict:
    total = 0
    for s in outcome["steps"]:
        total += s["prompt_tokens"] + s["completion_tokens"]
    outcome["total_tokens"] = total
    return outcome
