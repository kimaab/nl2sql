"""메타데이터 보강: LLM 이 테이블 카드·지표 예시 질문 초안을 만들고, 사람이 승인한 것만 질문 처리에 쓴다.

대기열(enrich_job)은 동기화(새·바뀐 테이블), 지표 저장, 테이블 용도 변경, 반려가 채운다.
"""
import logging
import re
import threading
from uuid import UUID

import db
from metrics import refresh_statuses
from models import ApiException, EnrichStatus, ReviewDecision, ReviewItem
from selector import Step, ask_json, model_name
from systems import structure_hash

log = logging.getLogger("nl2sql.enrich")

MAX_ATTEMPTS = 3          # 처음 1번 + 실패 이유를 붙여 다시 2번
CARD_LINE_MAX = 80
EXAMPLES_MIN, EXAMPLES_MAX = 3, 5

_running: set = set()     # 보강을 돌고 있는 시스템
_lock = threading.Lock()

TABLE_SYSTEM = """당신은 업무 데이터베이스의 테이블을 설명하는 카드를 씁니다. 이 카드는 질문에 맞는 테이블을 고를 때 쓰입니다.
[규칙]
- card: 세 부분을 줄바꿈으로 나눠 씁니다.
  무엇을 기록하나: 한두 문장
  주요 컬럼: 질문에 자주 쓰일 컬럼 3~8개를 `컬럼명` (뜻) 형식으로. 컬럼명은 반드시 [컬럼] 에 있는 것만, 백틱으로 감쌉니다.
  자주 나올 질문: 사람이 실제로 물을 법한 질문 2~3개
- card_line: 테이블 목록에 한 줄로 실릴 요약. {limit}자 이내. 테이블 이름을 되풀이하지 말고 무엇이 들어 있는지 씁니다.
- 컬럼·코멘트·용도에 없는 사실을 지어내지 않습니다.
- 설명 없이 JSON 객체 하나로만 답합니다: {{"card": "...", "card_line": "..."}}"""

METRIC_SYSTEM = """당신은 업무 지표마다 사람이 실제로 물을 법한 질문을 씁니다. 이 질문들은 질문에 맞는 지표를 고를 때 단서로 쓰입니다.
[규칙]
- 질문 {lo}~{hi}개.
- 지표 이름을 그대로 쓰지 마십시오. 같은 뜻의 다른 말, 구어체, 줄임말, 다른 업무 용어를 섞습니다.
- 기간·대상·정렬 같은 조건을 다양하게 섞어도 됩니다 (예: 어제, 차량별, 가장 많은).
- 이미 있는 질문과 겹치지 않게 씁니다.
- 설명 없이 JSON 객체 하나로만 답합니다: {{"questions": ["...", "..."]}}"""


# ── 대기열 실행 ───────────────────────────────────────────────────────────

def start(system_id: UUID, model) -> bool:
    """시스템의 대기열을 백그라운드에서 처리한다. 이미 돌고 있으면 False."""
    with _lock:
        if system_id in _running:
            return False
        _running.add(system_id)
    threading.Thread(target=_run_safely, args=(system_id, model), name=f"enrich-{system_id}", daemon=True).start()
    return True


def _run_safely(system_id: UUID, model) -> None:
    try:
        run(system_id, model)
    except Exception:
        log.exception("보강 실행 중 오류: %s", system_id)
    finally:
        with _lock:
            _running.discard(system_id)


def run(system_id: UUID, model, limit: int | None = None) -> int:
    """대기 중인 작업을 하나씩 처리한다. 처리한 수를 돌려준다."""
    done = 0
    while limit is None or done < limit:
        job = _claim(system_id)
        if job is None:
            break
        try:
            if job["target_type"] == "table":
                _enrich_table(job, model)
            else:
                _enrich_metric(job, model)
            _finish(job["id"], "done", None, model_name(model))
        except Exception as error:
            log.warning("보강 실패 (%s %s): %s", job["target_type"], job["target_id"], error)
            _finish(job["id"], "failed", str(error)[:500], model_name(model))
        done += 1
    if done:
        refresh_statuses(system_id)
        log.info("보강 %d건 처리: %s", done, system_id)
    return done


def _claim(system_id: UUID) -> dict | None:
    """대기 중인 작업 하나를 running 으로 바꿔 가져온다 (다른 작업자와 겹치지 않게 SKIP LOCKED)."""
    with db.connection() as conn:
        cur = conn.execute("""
            UPDATE enrich_job SET status = 'running', attempts = attempts + 1
             WHERE id = (
                SELECT j.id FROM enrich_job j
                  LEFT JOIN meta_table t ON j.target_type = 'table' AND t.id = j.target_id
                  LEFT JOIN metric m ON j.target_type = 'metric' AND m.id = j.target_id
                 WHERE j.status = 'queued' AND coalesce(t.system_id, m.system_id) = %s
                 ORDER BY j.created_at LIMIT 1 FOR UPDATE OF j SKIP LOCKED)
            RETURNING id, target_type, target_id, reason, note
        """, (str(system_id),))
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(("id", "target_type", "target_id", "reason", "note"), row))


def _finish(job_id: int, status: str, error: str | None, model: str) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE enrich_job SET status = %s, error = %s, model = %s, finished_at = now() WHERE id = %s",
                     (status, error, model, job_id))


def _with_retries(model, system: str, user: str, validate) -> dict:
    """형식 검증에 실패하면 실패 이유를 붙여 다시 만든다 (최대 MAX_ATTEMPTS 번)."""
    error = None
    for _ in range(MAX_ATTEMPTS):
        prompt = user if error is None else f"{user}\n\n[이전 답의 문제]\n{error}\n문제를 고쳐 다시 쓰십시오."
        try:
            return ask_json(model, system, prompt, Step("enrich"), validate)
        except ValueError as e:
            error = str(e)
    raise ValueError(f"{MAX_ATTEMPTS}번 만들었지만 형식 검증을 통과하지 못했습니다: {error}")


# ── 테이블 카드 ───────────────────────────────────────────────────────────

def _enrich_table(job: dict, model) -> None:
    table = db.one("SELECT * FROM meta_table WHERE id = %s AND deleted_at IS NULL", str(job["target_id"]))
    if table is None:
        return
    columns = db.query("""
        SELECT name, data_type, comment, is_pk FROM meta_column
         WHERE table_id = %s AND deleted_at IS NULL ORDER BY ordinal
    """, str(job["target_id"]))
    names = {c["name"] for c in columns}
    user = (
        f"[테이블] {table['name']}\n[용도] {table['purpose'] or '(없음)'}\n[코멘트] {table['comment'] or '(없음)'}\n"
        "[컬럼]\n" + "\n".join(
            f"- {c['name']} {c['data_type']}{' PK' if c['is_pk'] else ''}{': ' + c['comment'] if c['comment'] else ''}"
            for c in columns)
    )
    if job["note"]:
        user += f"\n\n[이전 초안 반려 사유]\n{job['note']}"

    def validate(p):
        card, line = str(p.get("card") or "").strip(), str(p.get("card_line") or "").strip()
        if not card or not line:
            raise ValueError('"card" 와 "card_line" 이 모두 필요합니다')
        if len(line) > CARD_LINE_MAX:
            raise ValueError(f"card_line 이 {len(line)}자입니다 ({CARD_LINE_MAX}자 이내)")
        unknown = [n for n in re.findall(r"`([^`]+)`", card) if n not in names]
        if unknown:
            raise ValueError(f"[컬럼] 에 없는 컬럼을 썼습니다: {', '.join(unknown[:5])}")

    parsed = _with_retries(model, TABLE_SYSTEM.format(limit=CARD_LINE_MAX), user, validate)
    with db.connection() as conn:
        conn.execute("""
            UPDATE meta_table SET card = %s, card_line = %s, card_status = 'draft', card_source_hash = %s,
                   updated_at = now()
             WHERE id = %s
        """, (parsed["card"].strip(), parsed["card_line"].strip(),
              structure_hash(table["purpose"], [(c["name"], c["data_type"]) for c in columns]), table["id"]))


# ── 지표 예시 질문 ────────────────────────────────────────────────────────

def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _enrich_metric(job: dict, model) -> None:
    metric = db.one("""
        SELECT m.*, t.name AS table_name FROM metric m JOIN meta_table t ON t.id = m.base_table_id WHERE m.id = %s
    """, str(job["target_id"]))
    if metric is None or metric["status"] == "retired":
        return
    existing = [r["question"] for r in db.query(
        "SELECT question FROM metric_example WHERE metric_id = %s AND status IN ('approved', 'draft')", str(metric["id"]))]
    definition = {
        "aggregate": f"{metric['agg_function']}({metric['agg_field']})",
        "projection": f"컬럼 조회: {', '.join(metric['select_columns'] or [])}",
        "derived": f"수식: {metric['expression']}",
    }.get(metric["kind"], metric["kind"])
    user = (
        f"[지표] {metric['name']}\n[설명] {metric['description'] or '(없음)'}\n"
        f"[정의] {metric['table_name']} 테이블 · {definition}\n"
        f"[동의어] {', '.join(metric['synonyms'] or []) or '(없음)'}\n"
        f"[이미 있는 질문]\n" + ("\n".join(f"- {q}" for q in existing) or "(없음)")
    )
    if job["note"]:
        user += f"\n\n[이전 초안 반려 사유]\n{job['note']}"
    taken = {_squash(q) for q in existing}
    name = _squash(metric["name"])

    def validate(p):
        questions = [str(q).strip() for q in p.get("questions") or [] if str(q).strip()]
        if not EXAMPLES_MIN <= len(questions) <= EXAMPLES_MAX:
            raise ValueError(f"질문이 {len(questions)}개입니다 ({EXAMPLES_MIN}~{EXAMPLES_MAX}개)")
        copied = [q for q in questions if name and name in _squash(q)]
        if copied:
            raise ValueError(f"지표 이름 '{metric['name']}' 을 그대로 쓴 질문이 있습니다: {copied[0]}")
        dup = [q for q in questions if _squash(q) in taken]
        if dup:
            raise ValueError(f"이미 있는 질문과 같습니다: {dup[0]}")

    parsed = _with_retries(model, METRIC_SYSTEM.format(lo=EXAMPLES_MIN, hi=EXAMPLES_MAX), user, validate)
    questions = [str(q).strip() for q in parsed["questions"] if str(q).strip()]
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO metric_example (metric_id, question, origin, status) VALUES (%s, %s, 'llm', 'draft')",
                            [(str(metric["id"]), q) for q in questions])


# ── 상태 · 검수 ──────────────────────────────────────────────────────────

def status(system_id: UUID) -> EnrichStatus:
    row = db.one("""
        SELECT
          count(*) FILTER (WHERE j.status = 'queued')  AS queued,
          count(*) FILTER (WHERE j.status = 'running') AS running,
          count(*) FILTER (WHERE j.status = 'failed')  AS failed
          FROM enrich_job j
          LEFT JOIN meta_table t ON j.target_type = 'table' AND t.id = j.target_id
          LEFT JOIN metric m ON j.target_type = 'metric' AND m.id = j.target_id
         WHERE coalesce(t.system_id, m.system_id) = %s
    """, str(system_id))
    drafts = db.one("""
        SELECT (SELECT count(*) FROM meta_table WHERE system_id = %s AND card_status = 'draft' AND deleted_at IS NULL)
                 AS cards,
               (SELECT count(*) FROM metric_example e JOIN metric m ON m.id = e.metric_id
                 WHERE m.system_id = %s AND e.status = 'draft') AS examples,
               (SELECT count(*) FROM eval_case WHERE system_id = %s AND status = 'draft') AS eval_cases
    """, str(system_id), str(system_id), str(system_id))
    return EnrichStatus(queued=row["queued"], running=row["running"], failed=row["failed"],
                        draft_cards=drafts["cards"], draft_examples=drafts["examples"],
                        draft_eval_cases=drafts["eval_cases"])


def enqueue_all(system_id: UUID, include_done: bool = False) -> int:
    """카드가 없는 테이블과 예시 질문이 없는 지표를 대기열에 넣는다 (처음 한 번, 또는 실패분 다시)."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO enrich_job (target_type, target_id, reason)
                SELECT 'table', t.id, 'new' FROM meta_table t
                 WHERE t.system_id = %s AND t.deleted_at IS NULL AND (%s OR t.card_status = 'none')
                ON CONFLICT (target_type, target_id) WHERE status = 'queued' DO NOTHING
            """, (str(system_id), include_done))
            tables = cur.rowcount
            cur.execute("""
                INSERT INTO enrich_job (target_type, target_id, reason)
                SELECT 'metric', m.id, 'new' FROM metric m
                 WHERE m.system_id = %s AND m.status IN ('draft', 'active')
                   AND (%s OR NOT EXISTS (SELECT 1 FROM metric_example e WHERE e.metric_id = m.id
                                           AND e.status IN ('approved', 'draft')))
                ON CONFLICT (target_type, target_id) WHERE status = 'queued' DO NOTHING
            """, (str(system_id), include_done))
            return tables + cur.rowcount


def list_review(system_id: UUID) -> list[ReviewItem]:
    items = [
        ReviewItem(kind="table_card", id=str(r["id"]), target_name=r["name"],
                   context=" · ".join(x for x in (r["purpose"], r["comment"]) if x) or "(용도·코멘트 없음)",
                   draft=r["card"], draft_line=r["card_line"])
        for r in db.query("""
            SELECT id, name, purpose, comment, card, card_line FROM meta_table
             WHERE system_id = %s AND card_status = 'draft' AND deleted_at IS NULL ORDER BY name
        """, str(system_id))
    ]
    items += [
        ReviewItem(kind="metric_example", id=str(r["id"]), target_name=r["name"],
                   context=r["description"] or "(설명 없음)", draft=r["question"])
        for r in db.query("""
            SELECT e.id, e.question, m.name, m.description FROM metric_example e JOIN metric m ON m.id = e.metric_id
             WHERE m.system_id = %s AND e.status = 'draft' ORDER BY m.name, e.id
        """, str(system_id))
    ]
    items += [
        ReviewItem(kind="eval_case", id=str(r["id"]), target_name=r["name"] or "(맞는 지표 없음)",
                   context=f"말투: {r['style'] or '-'} · 정답 지표가 맞는지, 실제로 물을 법한지 보십시오",
                   draft=r["question"])
        for r in db.query("""
            SELECT c.id, c.question, c.style, m.name FROM eval_case c LEFT JOIN metric m ON m.id = c.expected_metric_id
             WHERE c.system_id = %s AND c.status = 'draft' ORDER BY m.name, c.id
        """, str(system_id))
    ]
    return items


def decide(system_id: UUID, kind: str, item_id: str, decision: ReviewDecision) -> None:
    if kind == "table_card":
        _decide_table(system_id, UUID(item_id), decision)
    elif kind == "metric_example":
        _decide_example(system_id, int(item_id), decision)
    elif kind == "eval_case":
        _decide_eval_case(system_id, int(item_id), decision)
    else:
        raise ApiException(400, f"알 수 없는 검수 대상입니다: {kind}")


def _decide_eval_case(system_id: UUID, case_id: int, d: ReviewDecision) -> None:
    with db.connection() as conn:
        cur = conn.execute("""
            UPDATE eval_case SET status = %s, question = COALESCE(%s, question)
             WHERE id = %s AND system_id = %s AND status = 'draft' RETURNING id
        """, ("approved" if d.action == "approve" else "rejected", (d.question or "").strip() or None,
              case_id, str(system_id)))
        if cur.fetchone() is None:
            raise ApiException(404, "검수할 평가 문항이 없습니다")


def _decide_table(system_id: UUID, table_id: UUID, d: ReviewDecision) -> None:
    row = db.one("SELECT id FROM meta_table WHERE id = %s AND system_id = %s AND card_status = 'draft'",
                 str(table_id), str(system_id))
    if row is None:
        raise ApiException(404, "검수할 테이블 카드가 없습니다")
    with db.connection() as conn:
        if d.action == "approve":
            line = (d.card_line or "").strip()
            if line and len(line) > CARD_LINE_MAX:
                raise ApiException(400, f"한 줄 요약은 {CARD_LINE_MAX}자 이내여야 합니다")
            conn.execute("""
                UPDATE meta_table SET card = COALESCE(%s, card), card_line = COALESCE(%s, card_line),
                       card_status = 'approved', updated_at = now()
                 WHERE id = %s
            """, ((d.card or "").strip() or None, line or None, str(table_id)))
        else:
            conn.execute("UPDATE meta_table SET card_status = 'none', updated_at = now() WHERE id = %s", (str(table_id),))
            conn.execute("""
                INSERT INTO enrich_job (target_type, target_id, reason, note) VALUES ('table', %s, 'rejected', %s)
                ON CONFLICT (target_type, target_id) WHERE status = 'queued' DO NOTHING
            """, (str(table_id), d.note.strip()))


def _decide_example(system_id: UUID, example_id: int, d: ReviewDecision) -> None:
    row = db.one("""
        SELECT e.id, e.metric_id FROM metric_example e JOIN metric m ON m.id = e.metric_id
         WHERE e.id = %s AND m.system_id = %s AND e.status = 'draft'
    """, example_id, str(system_id))
    if row is None:
        raise ApiException(404, "검수할 예시 질문이 없습니다")
    with db.connection() as conn:
        if d.action == "approve":
            conn.execute("""
                UPDATE metric_example SET status = 'approved', question = COALESCE(%s, question),
                       reviewed_by = %s, reviewed_at = now()
                 WHERE id = %s
            """, ((d.question or "").strip() or None, d.reviewer or None, example_id))
        else:
            conn.execute("UPDATE metric_example SET status = 'rejected', reviewed_by = %s, reviewed_at = now() WHERE id = %s",
                         (d.reviewer or None, example_id))
            if d.note.strip():
                conn.execute("""
                    INSERT INTO enrich_job (target_type, target_id, reason, note) VALUES ('metric', %s, 'rejected', %s)
                    ON CONFLICT (target_type, target_id) WHERE status = 'queued' DO NOTHING
                """, (str(row["metric_id"]), d.note.strip()))
    refresh_statuses(system_id)
