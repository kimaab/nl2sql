"""질문에 쓸 테이블을 고르고, 지표가 필요한 질문인지 판단한다 (LLM).

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
STAGE_NAMES = {"table": "테이블 선택", "sql": "조회 명세", "draft": "지표 초안"}


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
    reply = model.invoke(messages)
    elapsed = int((time.monotonic() - started) * 1000)
    p, c = usage_of(reply)
    step.elapsed_ms += elapsed
    step.prompt_tokens += p
    step.completion_tokens += c
    log.info("%s %s 응답 attempt=%d (토큰 %d+%d, %dms)\n%s", LLM_LOG, stage, attempt, p, c, elapsed, reply.content)
    return reply


def ask_json(model, system: str, user: str, step: Step, validate, tries: int = 2) -> dict:
    """JSON 으로 답하게 하고, 형식이 틀리면 이유를 붙여 다시 묻는다. validate 는 고친 값을 돌려줘도 된다."""
    messages = [SystemMessage(system), HumanMessage(user)]
    last_error = None
    for attempt in range(1, tries + 1):
        reply = chat(model, messages, step, attempt)
        try:
            parsed = parse_json(reply.content)
            return validate(parsed) or parsed
        except (ValueError, KeyError, TypeError) as error:
            log.info("%s 응답 형식 오류 attempt=%d: %s", LLM_LOG, attempt, error)
            last_error = error
            messages += [
                reply,
                HumanMessage(f"형식이 틀렸습니다: {error}. 설명 없이 JSON 객체 하나만 다시 답하십시오."),
            ]
    raise ValueError(f"{STAGE_NAMES.get(step.stage, step.stage)} 응답을 읽지 못했습니다: {last_error}")


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


# ── 테이블 선택 + 지표 필요 판단 ──────────────────────────────────────────

TABLE_SYSTEM = """당신은 업무 시스템 '{name}' 의 테이블 목록에서 질문에 답할 테이블을 고르고, 지표가 필요한 질문인지 판단하는 역할입니다.
[업무 영역]
{domain}

[규칙]
- 질문에 답하는 데 꼭 필요한 테이블만 고릅니다 (보통 하나, 조인이 필요할 때만 여럿). 목록의 테이블 이름을 그대로 적습니다.
- 당신이 할 수 없는 행동을 제안하지 않습니다.
- 컬럼 뒤의 [계산 필요] 는 값을 그대로 합치거나 평균내면 틀리는 컬럼입니다 (예: 계속 커지는 누적값).
  이 컬럼으로 합계·사용량·기간 증감·비율처럼 계산해야 답할 수 있는 질문은 지표가 필요한 질문입니다 (needs_metric: true).
  값을 그대로 보여 주기만 하면 (목록, 마지막 값 등) 지표가 필요하지 않습니다.
- 지표가 필요한 질문이면 질문이 묻는 계산마다 그 테이블의 [지표] 중 맞는 지표를 골라 metrics 에 이름을 그대로 적습니다.
  계산이 여러 개면 (예: 무인 가동시간과 유인 가동시간) 지표도 모두 적습니다.
  계산 중 하나라도 맞는 지표가 없으면 metrics 는 빈 목록이고, reason 에 어떤 계산의 지표가 없는지 적습니다.
  그리고 missing 에 지표가 없는 계산마다 {{"name": "계산 이름", "table": "테이블", "column": "계산할 [계산 필요] 컬럼"}} 을 적습니다.
  name 은 기간·묶음·합계 같은 말을 뺀 계산 자체의 이름입니다 (예: '최근 7일 차량별 무인 절단 시간 합계' → '무인 절단 시간').
- 질문이 여러 갈래로 읽혀 정할 수 없을 때만 clarify 에 사용자에게 물을 한 문장을 적습니다. 그 밖에는 빈 문자열입니다.
- 설명 없이 JSON 객체 하나로만 답합니다:
  {{"tables": ["테이블"], "needs_metric": false, "metrics": [], "missing": [], "reason": "판단 이유", "clarify": ""}}"""


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
        if not picked and not str(p.get("clarify") or "").strip():
            raise ValueError('"tables" 가 비었습니다')
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
        return {**p, "tables": picked, "metrics": chosen, "missing": missing}

    system = TABLE_SYSTEM.format(name=contract.get("name", ""), domain=contract.get("domain") or "(설명 없음)")
    user = (
        "[테이블 목록]\n"
        + "\n".join(blocks)
        + f"\n\n[질문]\n{question}"
    )
    parsed = ask_json(model, system, user, step, validate)
    reason = str(parsed.get("reason") or "")
    clarify = str(parsed.get("clarify") or "").strip() or None
    needs_metric = bool(parsed.get("needs_metric")) or bool(parsed["metrics"])
    step.selected = list(parsed["tables"])
    for name in parsed["metrics"]:
        step.selected.append(f"지표: {name}")
    step.reason = reason
    missing = parsed["missing"] if needs_metric and not parsed["metrics"] else []
    log.info(
        "%s 테이블 선택 결과: tables=%s needs_metric=%s metrics=%s%s%s",
        LLM_LOG, parsed["tables"], needs_metric, parsed["metrics"],
        f" 없는 지표={missing}" if missing else "", f" 되묻기={clarify}" if clarify else "",
    )
    return Selection(
        parsed["tables"], parsed["metrics"], needs_metric, reason, step, clarification=clarify, missing=missing
    )
