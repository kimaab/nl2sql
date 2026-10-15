"""질문에 쓸 지표를 고른다.

TO-BE (llm): 시스템의 지표 목록에서 LLM 이 지표 하나를 고른다. 지표가 테이블을 정한다.
기준선 (tfidf): 테이블·지표 전체를 TF-IDF 로 검색해 상위 N개 — RAG 식 '한 번에 검색' 과 같은 방식. 비교용으로 남긴다.

두 방식 모두 계약서(dict)만 보고 돈다 — DB 없이 평가셋으로 비교할 수 있게.
"""

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage

from compiler import MENTION_PREFIX, _squash, mentioned_formulas
from pruner import Pruner

log = logging.getLogger("nl2sql.selector")



@dataclass
class Step:
    """ask_trace 한 행"""

    stage: str  # table | metric | sql
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
    metrics: list  # 고른 지표 이름
    pruned: dict  # 프롬프트에 실을 {tables, metrics, relations}
    steps: list  # [Step]
    clarification: str | None = None


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


LLM_LOG = "[LLM 연동]"
STAGE_NAMES = {"table": "테이블 선택", "metric": "지표 선택"}


def ask_json(model, system: str, user: str, step: Step, validate) -> dict:
    """JSON 으로 답하게 하고, 형식이 틀리면 이유를 붙여 한 번 더 묻는다. 토큰·시간은 step 에 더한다."""
    messages = [SystemMessage(system), HumanMessage(user)]
    stage = STAGE_NAMES.get(step.stage, step.stage)
    last_error = None
    for attempt in range(1, 3):
        if attempt == 1:
            log.info(
                "%s %s 요청 attempt=%d model=%s\n[system]\n%s\n[user]\n%s",
                LLM_LOG,
                stage,
                attempt,
                step.model,
                system,
                user,
            )
        else:
            log.info(
                "%s %s 재요청 attempt=%d\n[user]\n%s",
                LLM_LOG,
                stage,
                attempt,
                messages[-1].content,
            )
        started = time.monotonic()
        reply = model.invoke(messages)
        elapsed = int((time.monotonic() - started) * 1000)
        step.elapsed_ms += elapsed
        p, c = usage_of(reply)
        step.prompt_tokens += p
        step.completion_tokens += c
        log.info(
            "%s %s 응답 attempt=%d (토큰 %d+%d, %dms)\n%s",
            LLM_LOG,
            stage,
            attempt,
            p,
            c,
            elapsed,
            reply.content,
        )
        try:
            parsed = parse_json(reply.content)
            validate(parsed)
            return parsed
        except (ValueError, KeyError, TypeError) as error:
            log.info(
                "%s %s 응답 형식 오류 attempt=%d: %s", LLM_LOG, stage, attempt, error
            )
            last_error = error
            messages += [
                reply,
                HumanMessage(
                    f"형식이 틀렸습니다: {error}. 설명 없이 JSON 객체 하나만 다시 답하십시오."
                ),
            ]
    raise ValueError(f"{step.stage} 단계 응답을 읽지 못했습니다: {last_error}")


def prompt_table(t: dict) -> dict:
    """프롬프트에 싣는 테이블 모양 (내부 id·카드 상태는 뺀다)"""
    out = {"name": t["name"], "description": t.get("description", "")}
    if t.get("purpose"):
        out["purpose"] = t["purpose"]
    out["columns"] = t["columns"]
    return out


def _pruned(contract: dict, table_names: list, metric_names: list) -> dict:
    by_table = {t["name"]: t for t in contract["tables"]}
    by_metric = {m["name"]: m for m in contract["metrics"]}
    metrics = [by_metric[n] for n in metric_names if n in by_metric]
    names: list = []
    for n in list(table_names) + [
        x for m in metrics for x in m.get("tables", [m["table"]])
    ]:
        if n in by_table and n not in names:
            names.append(n)
    relations = [
        r
        for r in contract.get("relations", [])
        if r["left_table"] in names and r["right_table"] in names
    ]
    return {
        "tables": [prompt_table(by_table[n]) for n in names],
        "metrics": metrics,
        "relations": relations,
    }


def _match(names, allowed: list) -> list:
    """모델이 적은 이름을 실제 이름으로 (대소문자 무시). 없는 이름은 버린다."""
    lookup = {a.lower(): a for a in allowed}
    out = []
    for n in names or []:
        real = lookup.get(str(n).strip().lower())
        if real and real not in out:
            out.append(real)
    return out


# ── TO-BE: LLM 지표 선택 ──────────────────────────────────────────────────
# 지표 = 테이블(조인) + 디멘젼 + 허용 계산식. 질문 → 지표 하나 → 조회. 테이블을 따로 고르지 않는다.

METRIC_SYSTEM = """당신은 업무 시스템 '{name}' 의 업무 지표 목록에서, 사용자 질문에 답할 지표 하나를 고르는 역할입니다.
[업무 영역]
{domain}

[규칙]
- 아래 [지표 목록] 의 '지표명:' 뒤에 적힌 이름 하나를 그대로 적습니다 (설명을 적지 마십시오).
- 지표는 테이블과 계산식의 묶음입니다. 질문이 묻는 값이 그 지표의 테이블·계산식으로 계산되는지로 고르십시오.
  정렬(가장 큰, 상위 N)·기간·개수·필터 조건은 다음 단계에서 정하니 무시하십시오.
- 질문에 '{prefix} <이름>' 이 있으면 그 계산식을 허용하는 지표만 고를 수 있습니다.
- 맞는 지표가 없으면 metric 을 null 로 돌려줍니다.
- 두 지표 중 어느 쪽인지 질문만으로 정할 수 없으면 ambiguous 를 true 로 하고, options 에 후보를, message 에 사용자에게 물을 한 문장을 적습니다.
- 설명 없이 JSON 객체 하나로만 답합니다:
  {{"metric": "지표명", "reason": "고른 이유", "ambiguous": false, "options": [], "message": ""}}"""


def metric_block(m: dict, formulas: dict | None = None) -> str:
    """지표 선택에 보내는 지표 하나: 설명 · 같은 말 · 테이블 · 계산식"""
    lines = [
        f"- 지표명: {m['name']}",
        f"  설명: {' '.join(str(m.get('description') or '').split()) or '(설명 없음)'}",
    ]
    if m.get("synonyms"):
        lines.append(f"  같은 말: {', '.join(m['synonyms'])}")
    lines.append(f"  테이블: {', '.join(m.get('tables') or [m['table']])}")
    if m.get("formulas"):
        described = []
        for name in m["formulas"]:
            note = ((formulas or {}).get(name) or {}).get("description")
            described.append(f"{name}({' '.join(str(note).split())})" if note else name)
        lines.append(f"  계산식: {', '.join(described)}")
    return "\n".join(lines)


def _resolve_metric(value, candidates: list):
    """모델이 적은 지표를 실제 지표명으로. 띄어쓰기·대소문자를 무시하고, 이름 대신 설명을 적었으면 설명이 같은 지표로."""
    key = _squash(str(value or "")).lower()
    if not key:
        return None
    for m in candidates:
        if _squash(m["name"]).lower() == key:
            return m["name"]
    by_desc = [m["name"] for m in candidates if _squash(str(m.get("description") or "")).lower() == key]
    return by_desc[0] if len(by_desc) == 1 else None


def _allows(metric: dict, names: list) -> bool:
    have = {f.lower() for f in metric.get("formulas") or []}
    return all(n.lower() in have for n in names)


def select_metric(model, contract: dict, question: str) -> tuple[list, Step, str | None]:
    """→ ([고른 지표 하나] 또는 [], Step, 되물을 말).

    질문이 '계산된 X' 를 묻는데 고른 지표가 X 를 허용하지 않으면, X 를 허용하는 지표들 가운데서 다시 고르게 한다.
    """
    metrics = contract["metrics"]
    names = [m["name"] for m in metrics]
    step = Step("metric", candidates=names, model=model_name(model))
    if not metrics:
        step.reason = "등록된 지표가 없음"
        return [], step, "이 시스템에는 아직 등록된 지표가 없습니다. 지표를 먼저 등록하십시오."
    formulas = {f["name"]: f for f in contract.get("formulas", [])}
    mentioned = mentioned_formulas(question, formulas.values())
    system = METRIC_SYSTEM.format(
        name=contract.get("name", ""),
        domain=contract.get("domain") or "(설명 없음)",
        prefix=MENTION_PREFIX,
    )

    def validate(p):
        if "metric" not in p:
            raise ValueError('"metric" 이 필요합니다 (맞는 지표가 없으면 null)')
        if p["metric"] and not p.get("ambiguous") and _resolve_metric(p["metric"], candidates) is None:
            raise ValueError(
                f"'{p['metric']}' 는 [지표 목록] 에 없는 지표명입니다. '지표명:' 뒤의 이름을 그대로 적으십시오"
            )

    candidates = metrics
    note = ""
    if mentioned and not any(_allows(m, mentioned) for m in metrics):
        candidates = []
    for _ in range(2 if candidates else 0):
        user = (
            "[지표 목록]\n"
            + "\n".join(metric_block(m, formulas) for m in candidates)
            + note
            + f"\n\n[질문]\n{question}"
        )
        parsed = ask_json(model, system, user, step, validate)
        step.reason = str(parsed.get("reason") or "")
        allowed = [m["name"] for m in candidates]
        if parsed.get("ambiguous"):
            options = _match(parsed.get("options"), allowed)
            if len(options) >= 2:
                step.selected = []
                step.reason = step.reason or "질문만으로 지표를 정할 수 없음"
                message = str(parsed.get("message") or "어느 지표를 말씀하시나요?").strip()
                log.info("%s 지표 선택 결과: 되묻기 options=%s", LLM_LOG, options)
                return [], step, f"{message} (후보: {', '.join(options)})"
        resolved = _resolve_metric(parsed.get("metric"), candidates)
        picked = [resolved] if resolved else []
        if not picked:
            step.selected = []
            step.reason = step.reason or "맞는 지표가 없음"
            domain = contract.get("domain") or contract.get("name", "")
            return [], step, (
                f"질문에 맞는 지표를 찾지 못했습니다. 이 시스템은 {domain} 에 대한 것입니다. "
                "무엇을 조회하려는지 조금 더 구체적으로 적어 주십시오."
            )
        chosen = next(m for m in candidates if m["name"] == picked[0])
        if _allows(chosen, mentioned):
            step.selected = picked
            log.info("%s 지표 선택 결과: %s", LLM_LOG, picked)
            return picked, step, None
        # 계산식을 벗어난 지표 — 그 계산식을 허용하는 지표들 가운데서 다시 판단을 요청한다
        candidates = [m for m in metrics if _allows(m, mentioned)]
        log.info(
            "%s 지표 %s 는 계산식 %s 를 허용하지 않음 — 다시 고름 (후보 %s)",
            LLM_LOG,
            picked[0],
            mentioned,
            [m["name"] for m in candidates],
        )
        if not candidates:
            break
        note = (
            f"\n\n[다시 고르기]\n앞서 고른 '{picked[0]}' 는 질문의 계산식 "
            f"{', '.join(mentioned)} 을(를) 쓸 수 없습니다. 위 목록은 그 계산식을 쓸 수 있는 지표입니다."
        )
    step.selected = []
    wanted = ", ".join(f"'{MENTION_PREFIX} {n}'" for n in mentioned)
    return [], step, f"{wanted} 을(를) 계산할 수 있는 지표가 없습니다. 지표에 그 계산식을 허용해 주십시오."


def _pruned(contract: dict, metric_names: list) -> dict:
    """프롬프트에 실을 것: 고른 지표, 그 지표의 테이블, 허용 계산식"""
    by_table = {t["name"]: t for t in contract["tables"]}
    by_metric = {m["name"]: m for m in contract["metrics"]}
    metrics = [by_metric[n] for n in metric_names if n in by_metric]
    names = [n for m in metrics for n in m.get("tables", [m["table"]]) if n in by_table]
    allowed = {f.lower() for m in metrics for f in m.get("formulas") or []}
    return {
        "tables": [prompt_table(by_table[n]) for n in dict.fromkeys(names)],
        "metrics": metrics,
        "relations": [],
        "formulas": [f for f in contract.get("formulas", []) if f["name"].lower() in allowed],
    }


def llm_select(model, contract: dict, question: str) -> Selection:
    metrics, step, clarification = select_metric(model, contract, question)
    pruned = _pruned(contract, metrics)
    tables = [t["name"] for t in pruned["tables"]]
    log.info(
        "선택: 지표=%s 테이블=%s%s",
        metrics,
        tables,
        f" 되묻기={clarification}" if clarification else "",
    )
    return Selection(tables, metrics, pruned, [step], clarification=clarification)


# ── 기준선: TF-IDF (RAG 식 한 번에 검색) ──────────────────────────────────


def tfidf_select(contract: dict, question: str, top_k: int = 3) -> Selection:
    started = time.monotonic()
    pruned = Pruner(contract).prune(question, top_k=top_k)
    elapsed = int((time.monotonic() - started) * 1000)
    tables = [t["name"] for t in pruned["tables"]]
    metrics = [m["name"] for m in pruned["metrics"]]
    step = Step(
        "table",
        candidates=[t["name"] for t in contract["tables"]]
        + [m["name"] for m in contract["metrics"]],
        selected=tables + metrics,
        reason=f"TF-IDF 상위 {top_k}개",
        elapsed_ms=elapsed,
        model="tfidf",
    )
    pruned = {**pruned, "tables": [prompt_table(t) for t in pruned["tables"]]}
    return Selection(tables, metrics, pruned, [step])
