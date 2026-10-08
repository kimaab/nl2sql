"""질문에 쓸 테이블과 지표를 고른다.

TO-BE (llm): 테이블 추론(LLM) → 지표 후보 조회(테이블에 연결된 지표, 검색 아님) → 지표 선택(LLM).
기준선 (tfidf): 테이블·지표 전체를 TF-IDF 로 검색해 상위 N개 — RAG 식 '한 번에 검색' 과 같은 방식. 비교용으로 남긴다.

두 방식 모두 계약서(dict)만 보고 돈다 — DB 없이 평가셋으로 비교할 수 있게.
"""
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage

from pruner import Pruner

log = logging.getLogger("nl2sql.selector")

MAX_TABLES = 5
MAX_METRICS = 2
EXAMPLES_PER_METRIC = 2


@dataclass
class Step:
    """ask_trace 한 행"""
    stage: str                  # table | metric | sql
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
    tables: list            # 고른 테이블 이름
    metrics: list           # 고른 지표 이름
    pruned: dict            # 프롬프트에 실을 {tables, metrics, relations}
    steps: list             # [Step]
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
    return str(getattr(model, "model_name", None) or getattr(model, "model", None) or type(model).__name__)


def parse_json(text: str) -> dict:
    """모델 응답에서 JSON 객체 하나를 꺼낸다 (코드펜스·앞뒤 말 허용)"""
    body = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("JSON 객체가 없습니다")
    parsed = json.loads(body[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("JSON 객체가 아닙니다")
    return parsed


def ask_json(model, system: str, user: str, step: Step, validate) -> dict:
    """JSON 으로 답하게 하고, 형식이 틀리면 이유를 붙여 한 번 더 묻는다. 토큰·시간은 step 에 더한다."""
    messages = [SystemMessage(system), HumanMessage(user)]
    last_error = None
    for _ in range(2):
        started = time.monotonic()
        reply = model.invoke(messages)
        # 테이블 산정 완료
        step.elapsed_ms += int((time.monotonic() - started) * 1000)
        p, c = usage_of(reply)
        step.prompt_tokens += p
        step.completion_tokens += c
        try:
            parsed = parse_json(reply.content)
            validate(parsed)
            return parsed
        except (ValueError, KeyError, TypeError) as error:
            last_error = error
            messages += [reply, HumanMessage(f"형식이 틀렸습니다: {error}. 설명 없이 JSON 객체 하나만 다시 답하십시오.")]
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
    for n in list(table_names) + [x for m in metrics for x in m.get("tables", [m["table"]])]:
        if n in by_table and n not in names:
            names.append(n)
    relations = [r for r in contract.get("relations", []) if r["left_table"] in names and r["right_table"] in names]
    return {"tables": [prompt_table(by_table[n]) for n in names], "metrics": metrics, "relations": relations}


def _match(names, allowed: list) -> list:
    """모델이 적은 이름을 실제 이름으로 (대소문자 무시). 없는 이름은 버린다."""
    lookup = {a.lower(): a for a in allowed}
    out = []
    for n in names or []:
        real = lookup.get(str(n).strip().lower())
        if real and real not in out:
            out.append(real)
    return out


# ── TO-BE: LLM 선택 ───────────────────────────────────────────────────────

TABLE_SYSTEM = """당신은 업무 시스템 '{name}' 의 테이블 목록에서, 사용자 질문에 답하는 데 필요한 테이블을 고르는 역할입니다.
[업무 영역]
{domain}

[규칙]
- 아래 [테이블 목록] 에 있는 이름만 씁니다.
- 놓치지 않는 것이 우선입니다. 필요할 수 있는 테이블은 넣으십시오 (최대 {max_tables}개).
- 질문의 기간·정렬·개수 같은 조건은 테이블을 고르는 데 쓰지 않습니다.
- 맞는 테이블이 없으면 빈 목록을 돌려줍니다.
- 설명 없이 JSON 객체 하나로만 답합니다:
  {{"tables": [{{"name": "테이블명", "reason": "고른 이유 한 줄"}}]}}"""

METRIC_SYSTEM = """당신은 사용자 질문이 묻는 '무엇'에 맞는 업무 지표를 고르는 역할입니다.
[규칙]
- 아래 [후보 지표] 에 있는 이름만 씁니다. 최대 {max_metrics}개.
- 지표는 '무엇을 세는가' 로 고릅니다. 정렬(가장 큰, 상위 N)·기간·개수·필터 조건은 다음 단계에서 정하니 무시하십시오.
- 맞는 지표가 없으면 빈 목록을 돌려줍니다 — 그러면 테이블을 직접 조회합니다.
- 두 지표 중 어느 쪽인지 질문만으로 정할 수 없으면 ambiguous 를 true 로 하고, options 에 후보를, message 에 사용자에게 물을 한 문장을 적습니다.
- 설명 없이 JSON 객체 하나로만 답합니다:
  {{"metrics": ["지표명"], "reason": "고른 이유", "ambiguous": false, "options": [], "message": ""}}"""


def glossary_block(contract: dict, question: str) -> str:
    """질문에 나온 업무 용어만 싣는다 (용어나 같은 말이 질문에 그대로 들어 있을 때)"""
    q = re.sub(r"\s+", "", question).lower()
    lines = []
    for g in contract.get("glossary") or []:
        words = [g["term"], *g.get("synonyms", [])]
        if not any(re.sub(r"\s+", "", w).lower() in q for w in words if w):
            continue
        target = ", ".join(
            f"{m['metric']} 지표" if m.get("metric") else ".".join(x for x in (m.get("table"), m.get("column")) if x)
            for m in g.get("maps_to") or [])
        line = f"- {g['term']}"
        if g.get("synonyms"):
            line += f" (= {', '.join(g['synonyms'])})"
        if g.get("meaning"):
            line += f": {g['meaning']}"
        if target:
            line += f" → {target}"
        lines.append(line)
    return "[업무 용어]\n" + "\n".join(lines) + "\n\n" if lines else ""


def table_line(t: dict) -> str:
    """테이블 추론에 보내는 한 줄. 승인된 카드 한 줄 → 용도 → 코멘트 순으로 쓴다."""
    text = t.get("card_line") or t.get("purpose") or t.get("description") or ""
    text = " ".join(str(text).split())
    return f"- {t['name']}: {text}" if text else f"- {t['name']}"


def metric_block(m: dict) -> str:
    lines = [f"- {m['name']}: {' '.join(str(m.get('description') or '').split()) or '(설명 없음)'}"]
    if m.get("synonyms"):
        lines.append(f"  같은 말: {', '.join(m['synonyms'])}")
    for e in (m.get("examples") or [])[:EXAMPLES_PER_METRIC]:
        lines.append(f"  예: \"{e['question']}\"")
    return "\n".join(lines)


def infer_tables(model, contract: dict, question: str) -> tuple[list, list, Step]:
    """→ (고른 테이블, [이유], Step)"""
    names = [t["name"] for t in contract["tables"]]
    step = Step("table", candidates=names, model=model_name(model))
    system = TABLE_SYSTEM.format(name=contract.get("name", ""), domain=contract.get("domain") or "(설명 없음)",
                                 max_tables=MAX_TABLES)
    user = (glossary_block(contract, question) + "[테이블 목록]\n"
            + "\n".join(table_line(t) for t in contract["tables"]) + f"\n\n[질문]\n{question}")

    def validate(p):
        if not isinstance(p.get("tables"), list):
            raise ValueError('"tables" 목록이 필요합니다')

    parsed = ask_json(model, system, user, step, validate)
    picked, reasons = [], []
    for item in parsed["tables"]:
        name = item.get("name") if isinstance(item, dict) else item
        real = _match([name], names)
        if real and real[0] not in picked:
            picked.append(real[0])
            reasons.append(f"{real[0]}: {(item.get('reason') if isinstance(item, dict) else '') or ''}".strip(": "))
    step.selected = picked[:MAX_TABLES]
    step.reason = " / ".join(reasons[:MAX_TABLES])
    return step.selected, reasons, step


def metric_candidates(contract: dict, tables: list) -> list:
    """고른 테이블에 연결된 지표 (metric_table). 검색이 아니라 조회다."""
    wanted = {t.lower() for t in tables}
    return [m for m in contract["metrics"] if wanted & {x.lower() for x in m.get("tables", [m["table"]])}]


def select_metrics(model, question: str, candidates: list, glossary: str = "") -> tuple[list, Step, str | None]:
    """→ (고른 지표, Step, 되물을 말)"""
    names = [m["name"] for m in candidates]
    step = Step("metric", candidates=names, model=model_name(model))
    if not candidates:
        step.reason = "고른 테이블에 연결된 지표가 없음 — 테이블을 직접 조회"
        return [], step, None
    system = METRIC_SYSTEM.format(max_metrics=MAX_METRICS)
    user = glossary + "[후보 지표]\n" + "\n".join(metric_block(m) for m in candidates) + f"\n\n[질문]\n{question}"

    def validate(p):
        if not isinstance(p.get("metrics"), list):
            raise ValueError('"metrics" 목록이 필요합니다')

    parsed = ask_json(model, system, user, step, validate)
    step.reason = str(parsed.get("reason") or "")
    if parsed.get("ambiguous"):
        options = _match(parsed.get("options"), names)
        if len(options) >= 2:
            step.selected = []
            step.reason = step.reason or "질문만으로 지표를 정할 수 없음"
            message = str(parsed.get("message") or "어느 지표를 말씀하시나요?").strip()
            return [], step, f"{message} (후보: {', '.join(options)})"
    step.selected = _match(parsed["metrics"], names)[:MAX_METRICS]
    return step.selected, step, None


def llm_select(model, contract: dict, question: str) -> Selection:
    tables, _, table_step = infer_tables(model, contract, question)
    if not tables:
        domain = contract.get("domain") or contract.get("name", "")
        return Selection([], [], {"tables": [], "metrics": [], "relations": []}, [table_step],
                         clarification=f"질문에 맞는 테이블을 찾지 못했습니다. 이 시스템은 {domain} 에 대한 것입니다. "
                                       "무엇을 조회하려는지 조금 더 구체적으로 적어 주십시오.")
    candidates = metric_candidates(contract, tables)
    metrics, metric_step, clarification = select_metrics(model, question, candidates,
                                                         glossary_block(contract, question))
    log.info("선택: 테이블=%s 지표=%s (후보 %d개)%s", tables, metrics, len(candidates),
             f" 되묻기={clarification}" if clarification else "")
    return Selection(tables, metrics, _pruned(contract, tables, metrics), [table_step, metric_step],
                     clarification=clarification)


# ── 기준선: TF-IDF (RAG 식 한 번에 검색) ──────────────────────────────────

def tfidf_select(contract: dict, question: str, top_k: int = 3) -> Selection:
    started = time.monotonic()
    pruned = Pruner(contract).prune(question, top_k=top_k)
    elapsed = int((time.monotonic() - started) * 1000)
    tables = [t["name"] for t in pruned["tables"]]
    metrics = [m["name"] for m in pruned["metrics"]]
    step = Step("table", candidates=[t["name"] for t in contract["tables"]] + [m["name"] for m in contract["metrics"]],
                selected=tables + metrics, reason=f"TF-IDF 상위 {top_k}개", elapsed_ms=elapsed, model="tfidf")
    pruned = {**pruned, "tables": [prompt_table(t) for t in pruned["tables"]]}
    return Selection(tables, metrics, pruned, [step])
