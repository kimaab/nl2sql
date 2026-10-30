"""질문을 조회 항목, 테이블, 묶음, 조건으로 나누고, 지표가 필요한 질문인지 판단한다 (LLM).

테이블마다 컬럼(계산 필요 표시 포함)과 그 테이블을 쓰는 지표의 설명을 함께 보낸다.
계약서(dict)만 보고 돈다 — DB 없이 평가셋으로 돌릴 수 있게.
"""

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage

log = logging.getLogger("nl2sql.selector")

LLM_LOG = "[LLM 연동]"
STAGE_NAMES = {"table": "질문 분석", "sql": "조회 명세", "draft": "지표 초안"}


@dataclass
class Step:
    """ask_trace 한 행"""

    stage: str  # table | sql | draft
    candidates: list = field(default_factory=list)
    selected: list = field(default_factory=list)
    reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0
    model: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Selection:
    tables: list  # 고른 테이블 이름
    metrics: list  # 고른 지표 이름 (질문이 묻는 계산마다 하나)
    needs_metric: bool  # 지표가 필요한 질문인가
    reason: str
    step: Step
    clarification: str | None = None
    missing: list = field(default_factory=list)  # 지표가 없는 계산 [{name, table, column}]
    # 질문 분석: {items, grouping, dimensions, conditions, ambiguous}. 다음 단계(AST)가 이대로 만든다
    analysis: dict = field(default_factory=dict)


# ── 공통 ──────────────────────────────────────────────────────────────────


def usage_of(message) -> tuple[int, int]:
    """AIMessage 의 토큰 수 (vLLM·OpenAI 호환 응답 모두)"""
    meta = getattr(message, "usage_metadata", None) or {}
    if meta:
        return int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0))
    usage = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
    return int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


def model_name(model) -> str:
    return str(
        getattr(model, "model_name", None)
        or getattr(model, "model", None)
        or type(model).__name__
    )


def parse_json(text: str) -> dict:
    """모델 응답에서 JSON 객체 하나를 꺼낸다 (코드펜스·앞뒤 말 허용)"""
    body = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("JSON 객체가 없습니다")
    parsed = json.loads(body[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("JSON 객체가 아닙니다")
    return parsed


# 단계별 출력 토큰 상한. 정상 응답은 질문 분석 3천, AST 4천 토큰 안쪽이다.
# 같은 내용을 되풀이하다 수만 토큰(몇 분)을 쓰는 일이 있어, 이 길이에서 끊고 다시 묻는다.
MAX_TOKENS = {"table": 3000, "sql": 6000}

TRUNCATED = "응답이 너무 길어 잘렸습니다. 같은 내용을 되풀이하지 말고 JSON 객체 하나로 짧게 다시 답하십시오."


def bound(model, stage: str):
    """단계의 출력 상한과 JSON 강제를 건 모델. 상한이 없는 단계나 bind 가 없는 모델(테스트)은 그대로"""
    limit = MAX_TOKENS.get(stage)
    if limit is None or not hasattr(model, "bind"):
        return model
    kwargs = {"max_tokens": limit}
    # vLLM 은 JSON 객체만 내게 강제한다. Claude 의 OpenAI 호환 API 는 지원이 달라 뺀다.
    # response_format 인자로 주면 langchain 이 잘린 응답에서 예외를 던져 다시 묻지 못한다 — 요청 본문에 직접 싣는다
    if not model_name(model).startswith("claude"):
        kwargs["extra_body"] = {"response_format": {"type": "json_object"}}
    return model.bind(**kwargs)


def truncated(reply) -> bool:
    """출력 상한에 걸려 응답이 잘렸는가"""
    meta = getattr(reply, "response_metadata", None) or {}
    return meta.get("finish_reason") in ("length", "max_tokens")


def follow_up(reply, text: str) -> list:
    """다시 물을 때 덧붙일 메시지. 잘린 응답은 되풀이를 부추기니 대화에 남기지 않는다"""
    if truncated(reply):
        return [HumanMessage(text)]
    return [reply, HumanMessage(text)]


def chat(model, messages: list, step: Step, attempt: int):
    """모델을 한 번 부른다. 요청·응답을 로그에 남기고 토큰·시간은 step 에 더한다."""
    stage = STAGE_NAMES.get(step.stage, step.stage)
    if attempt == 1:
        log.info(
            "%s %s 요청 attempt=1 model=%s\n[system]\n%s\n[user]\n%s",
            LLM_LOG, stage, step.model, messages[0].content, messages[-1].content,
        )
    else:
        log.info("%s %s 재요청 attempt=%d\n[user]\n%s", LLM_LOG, stage, attempt, messages[-1].content)
    started = time.monotonic()
    reply = bound(model, step.stage).invoke(messages)
    elapsed = int((time.monotonic() - started) * 1000)
    p, c = usage_of(reply)
    step.elapsed_ms += elapsed
    step.prompt_tokens += p
    step.completion_tokens += c
    log.info(
        "%s %s 응답 attempt=%d (토큰 %d+%d, %dms)%s\n%s",
        LLM_LOG, stage, attempt, p, c, elapsed, " 출력 상한에서 잘림" if truncated(reply) else "", reply.content,
    )
    return reply


def ask_json(model, system: str, user: str, step: Step, validate, tries: int = 2) -> dict:
    """JSON 으로 답하게 하고, 형식이 틀리면 이유를 붙여 다시 묻는다. validate 는 고친 값을 돌려줘도 된다."""
    messages = [SystemMessage(system), HumanMessage(user)]
    last_error = None
    for attempt in range(1, tries + 1):
        reply = chat(model, messages, step, attempt)
        try:
            if truncated(reply):
                raise ValueError(TRUNCATED)
            parsed = parse_json(reply.content)
            return validate(parsed) or parsed
        except (ValueError, KeyError, TypeError) as error:
            log.info("%s 응답 형식 오류 attempt=%d: %s", LLM_LOG, attempt, error)
            last_error = error
            if truncated(reply):
                messages += follow_up(reply, TRUNCATED)
            else:
                messages += follow_up(reply, f"형식이 틀렸습니다: {error}. 설명 없이 JSON 객체 하나만 다시 답하십시오.")
    raise ValueError(f"{STAGE_NAMES.get(step.stage, step.stage)} 응답을 읽지 못했습니다: {last_error}")


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


# ── 테이블 선택 + 지표 필요 판단 ──────────────────────────────────────────

TABLE_SYSTEM = """당신은 업무 시스템 '{name}' 의 질문을 조회 항목, 테이블, 묶음, 조건으로 나누고, 지표가 필요한 질문인지 판단하는 역할입니다.
[업무 영역]
{domain}

[질문 나누기]
질문에 있는 것만 적고 더하지 않습니다. 컬럼은 [테이블 목록] 의 '테이블.컬럼' 으로 적습니다.
- items (조회 항목): 결과에 보여 줄 값마다 {{"name": "질문의 말", "column": "테이블.컬럼"}}.
  계산한 값(합계, 상태 판정 등)은 계산에 쓰는 컬럼을 적고, 하나로 정할 수 없으면 column 은 빈 문자열입니다.
  질문이 '전체', '모든 컬럼', '다 보여줘' 처럼 보여 줄 컬럼을 정하지 않으면 테이블마다 {{"name": "전체", "column": "테이블.*"}} 로 적습니다.
- tables: 아래 규칙대로 고른 테이블입니다.
- grouping: 묶어서 집계(합계, 개수, 평균, 최댓값 등)해야 하면 true, 행을 그대로 보여 주면 false 입니다.
- dimensions (묶음 항목): grouping 이 true 일 때 묶는 기준마다 {{"name": "질문의 말", "column": "테이블.컬럼"}} (예: 차량별, 일별). false 면 빈 목록입니다.
- conditions (조건): 거르는 조건마다 {{"name": "질문의 말", "column": "테이블.컬럼", "value": "값"}} (기간, 아이디, 코드값, 권한 범위 등).
- ambiguous: 위 항목 중 질문만으로 정할 수 없는 것마다 {{"part": "조회 항목|테이블|묶음|묶음 항목|조건", "question": "사용자에게 물을 한 문장"}}.
  여러 갈래로 읽혀 결과가 달라질 때만 적습니다 (예: '가동시간' 이 엔진 가동시간인지 무인 가동시간인지 알 수 없을 때).
  시간에 따라 쌓이는 이력 테이블을 조회하는데 기간이 없으면 기간을 묻습니다 (전체 기간으로 정하지 않습니다).
  다만 '실시간', '현재', '지금', '최신', '마지막' 처럼 가장 최근 값을 뜻하는 말이 있으면 단위(예: 차량)별 마지막 한 건으로 보고 묻지 않습니다.
  이때 conditions 에 {{"name": "최신 값", "column": "기준 시각 컬럼", "value": "마지막 한 건"}} 을 적습니다.
  기준 정보처럼 기간과 상관없는 테이블(사용자, 차량 목록 등)만 볼 때는 기간을 묻지 않습니다. 정할 수 있으면 빈 목록입니다.

[규칙]
- 질문에 답하는 데 꼭 필요한 테이블만 고릅니다 (보통 하나, 조인이 필요할 때만 여럿). 목록의 테이블 이름을 그대로 적습니다.
- 당신이 할 수 없는 행동을 제안하지 않습니다.
- 컬럼 뒤의 [계산 필요] 는 값을 그대로 합치거나 평균내면 틀리는 컬럼입니다 (예: 계속 커지는 누적값).
  이 컬럼으로 합계·사용량·기간 증감·비율처럼 계산해야 답할 수 있는 질문은 지표가 필요한 질문입니다 (needs_metric: true).
  값을 그대로 보여 주기만 하면 (목록, 마지막 값 등) 지표가 필요하지 않습니다.
  [계산 필요] 컬럼으로 상위·하위 계층을 따라가는 것(예: parent_id 로 하위 그룹 전체)도 계산입니다. 그 계층을 쓰는 지표가 필요합니다.
- 지표가 필요한 질문이면 질문이 묻는 계산마다 그 테이블의 [지표] 중 맞는 지표를 골라 metrics 에 이름을 그대로 적습니다.
  계산이 여러 개면 (예: 무인 가동시간과 유인 가동시간) 지표도 모두 적습니다.
  계산 중 하나라도 맞는 지표가 없으면 metrics 는 빈 목록이고, reason 에 어떤 계산의 지표가 없는지 적습니다.
  그리고 missing 에 지표가 없는 계산마다 {{"name": "계산 이름", "table": "테이블", "column": "계산할 [계산 필요] 컬럼"}} 을 적습니다.
  name 은 기간·묶음·합계 같은 말을 뺀 계산 자체의 이름입니다 (예: '최근 7일 차량별 무인 절단 시간 합계' → '무인 절단 시간').
- 설명 없이 JSON 객체 하나로만 답합니다:
  {{"items": [], "tables": ["테이블"], "grouping": false, "dimensions": [], "conditions": [], "ambiguous": [],
   "needs_metric": false, "metrics": [], "missing": [], "reason": "판단 이유"}}"""

# ambiguous 의 part 로 받는 이름
PARTS = ("조회 항목", "테이블", "묶음", "묶음 항목", "조건")


def _column_ref(text, names: dict, columns: dict, picked: list) -> tuple[str, str]:
    """'테이블.컬럼' → (테이블, 컬럼). 컬럼이 '*' 면 (테이블, '*'). 모르는 이름은 빈 값.
    테이블 없이 컬럼만 적었으면 고른 테이블 중 그 컬럼이 하나뿐일 때 그 테이블로 본다."""
    text = str(text or "").strip()
    if not text:
        return "", ""
    if "." not in text:
        if text == "*":
            return "", "*"
        found = []
        for t in picked:
            if text.lower() in columns[t]:
                found.append(t)
        if len(found) != 1:
            return "", ""
        return found[0], columns[found[0]][text.lower()]
    # 테이블 이름에 스키마의 점이 있을 수 있다 (violet.tb_users) — 마지막 점이 컬럼 앞이다
    table_part, column_part = text.rsplit(".", 1)
    table = names.get(table_part.strip().lower())
    if table is None:
        return "", ""
    if column_part.strip() == "*":
        return table, "*"
    return table, columns[table].get(column_part.strip().lower(), "")


def _parts(raw, names: dict, columns: dict, picked: list, with_value: bool = False) -> list:
    """items · dimensions · conditions → [{name, column('테이블.컬럼' | '*' | ''), value?}]. 틀린 항목은 다시 묻지 않고 버린다."""
    out = []
    if not isinstance(raw, list):
        return out
    for r in raw:
        if isinstance(r, str):
            r = {"name": r}
        if not isinstance(r, dict):
            continue
        name = " ".join(str(r.get("name") or "").split())
        table, column = _column_ref(r.get("column"), names, columns, picked)
        ref = f"{table}.{column}" if table and column else column
        if not name and not ref:
            continue
        part = {"name": name or ref, "column": ref}
        if with_value:
            value = r.get("value")
            if value is None:
                value = ""
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            part["value"] = " ".join(value.split())
        out.append(part)
    return out


def _ambiguous(p: dict) -> list:
    """ambiguous (예전 형식 clarify 포함) → [{part, question}]"""
    out = []
    raw = p.get("ambiguous")
    if not isinstance(raw, list):
        raw = []
    for a in raw:
        if isinstance(a, str):
            a = {"question": a}
        if not isinstance(a, dict):
            continue
        question = " ".join(str(a.get("question") or "").split())
        if not question:
            continue
        part = str(a.get("part") or "").strip()
        out.append({"part": part if part in PARTS else "", "question": question})
    clarify = " ".join(str(p.get("clarify") or "").split())
    if clarify:
        out.append({"part": "", "question": clarify})
    return out


def clarification_text(ambiguous: list) -> str | None:
    """사용자에게 물을 말. 하나면 그 문장, 여럿이면 항목마다 한 줄"""
    if not ambiguous:
        return None
    if len(ambiguous) == 1:
        return ambiguous[0]["question"]
    lines = []
    for a in ambiguous:
        lines.append(f"- {a['part']}: {a['question']}" if a["part"] else f"- {a['question']}")
    return "\n".join(lines)


def table_block(t: dict, metrics: list) -> str:
    """테이블 선택에 보내는 테이블 하나: 설명 · 컬럼(계산 필요 표시) · 지표"""
    note = t.get("purpose") or t.get("description") or "(설명 없음)"
    columns = []
    for c in t["columns"]:
        text = f"{c['name']}({c['description']})" if c.get("description") else c["name"]
        columns.append(text + (" [계산 필요]" if c.get("needs_calc") else ""))
    lines = [f"- {t['name']}: {' '.join(note.split())}", "  컬럼: " + ", ".join(columns)]
    for m in metrics:
        desc = " ".join(str(m.get("description") or "").split()) or "(설명 없음)"
        same = f" (같은 말: {', '.join(m['synonyms'])})" if m.get("synonyms") else ""
        lines.append(f"  [지표] {m['name']}: {desc}{same}")
    return "\n".join(lines)


def select_tables(model, contract: dict, question: str) -> Selection:
    tables = contract["tables"]
    metrics = contract.get("metrics") or []
    by_table: dict = {}
    for m in metrics:
        for name in m.get("tables") or []:
            by_table.setdefault(name, []).append(m)
    candidates, names, columns, blocks = [], {}, {}, []
    for t in tables:
        candidates.append(t["name"])
        names[t["name"].lower()] = t["name"]
        columns[t["name"]] = {}
        for c in t["columns"]:
            columns[t["name"]][c["name"].lower()] = c["name"]
        blocks.append(table_block(t, by_table.get(t["name"], [])))
    metric_names = {}
    for m in metrics:
        metric_names[_squash(m["name"])] = m
    step = Step("table", candidates=candidates, model=model_name(model))

    def validate(p):
        picked = []
        for n in p.get("tables") or []:
            real = names.get(str(n).strip().lower())
            if real is None:
                raise ValueError(f"'{n}' 는 [테이블 목록] 에 없는 테이블입니다")
            if real not in picked:
                picked.append(real)
        asked = p.get("metrics")
        if asked is None:
            asked = [p["metric"]] if p.get("metric") else []  # 예전 형식 {"metric": "이름"}
        if isinstance(asked, str):
            asked = [asked]
        chosen = []
        for n in asked:
            metric = metric_names.get(_squash(n))
            if metric is None:
                raise ValueError(f"'{n}' 는 [지표] 에 없는 지표 이름입니다. 이름을 그대로 적거나 빈 목록으로 답하십시오")
            if metric["name"] not in chosen:
                chosen.append(metric["name"])
                # 지표를 골랐으면 지표의 테이블도 쓴다
                for t in metric.get("tables") or []:
                    if t not in picked:
                        picked.append(t)
        ambiguous = _ambiguous(p)
        items = _parts(p.get("items"), names, columns, picked)
        # '전체' 를 고른 테이블은 컬럼 목록을 보여 줘야 하니 테이블에도 넣는다
        for item in items:
            if item["column"].endswith(".*") and item["column"][:-2] not in picked:
                picked.append(item["column"][:-2])
        if not picked and not ambiguous:
            raise ValueError('"tables" 가 비었습니다')
        grouping = p.get("grouping") is True
        analysis = {
            "items": items,
            "grouping": grouping,
            "dimensions": _parts(p.get("dimensions"), names, columns, picked) if grouping else [],
            "conditions": _parts(p.get("conditions"), names, columns, picked, with_value=True),
            "ambiguous": ambiguous,
        }
        # 없는 계산은 지표 초안의 출발점일 뿐이라 틀린 항목은 다시 묻지 않고 버린다
        missing = []
        for m in p.get("missing") or []:
            if not isinstance(m, dict) or not str(m.get("name") or "").strip():
                continue
            table = names.get(str(m.get("table") or "").strip().lower())
            if table is None:
                continue
            column = columns[table].get(str(m.get("column") or "").strip().lower(), "")
            missing.append({"name": str(m["name"]).strip(), "table": table, "column": column})
        return {**p, "tables": picked, "metrics": chosen, "missing": missing, "analysis": analysis}

    system = TABLE_SYSTEM.format(name=contract.get("name", ""), domain=contract.get("domain") or "(설명 없음)")
    user = (
        "[테이블 목록]\n"
        + "\n".join(blocks)
        + f"\n\n[질문]\n{question}"
    )
    parsed = ask_json(model, system, user, step, validate)
    reason = str(parsed.get("reason") or "")
    analysis = parsed["analysis"]
    clarify = clarification_text(analysis["ambiguous"])
    needs_metric = bool(parsed.get("needs_metric")) or bool(parsed["metrics"])
    step.selected = list(parsed["tables"])
    for name in parsed["metrics"]:
        step.selected.append(f"지표: {name}")
    step.reason = reason
    # 지표를 골랐어도 없는 계산이 남을 수 있다 — 고른 지표가 그 계산을 하는지는 ask.py 가 지표 AST 로 가린다
    missing = parsed["missing"] if needs_metric else []
    log.info(
        "%s 테이블 선택 결과: tables=%s needs_metric=%s metrics=%s 분석=%s%s%s",
        LLM_LOG, parsed["tables"], needs_metric, parsed["metrics"], json.dumps(analysis, ensure_ascii=False),
        f" 없는 지표={missing}" if missing else "", f" 되묻기={clarify}" if clarify else "",
    )
    return Selection(
        parsed["tables"], parsed["metrics"], needs_metric, reason, step,
        clarification=clarify, missing=missing, analysis=analysis,
    )
