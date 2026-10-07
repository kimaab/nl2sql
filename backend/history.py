import json
import logging
from uuid import UUID, uuid4

import db
from models import ApiException, AskStep, HistoryEntry, HistoryPatch

log = logging.getLogger("nl2sql.history")


def record_ask(system_id: UUID, question: str, outcome: dict, elapsed_ms: float) -> UUID | None:
    """질문 결과와 단계별 기록(ask_trace)을 남긴다. 기록이 실패해도 답은 돌려준다 — 기록은 부가 기능이다."""
    entry_id = uuid4()
    try:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ask_log (id, system_id, question, selected_tables, selected_metrics, ast, sql, error,
                                         clarification, attempts, total_tokens, elapsed_ms)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    str(entry_id), str(system_id), question,
                    json.dumps(outcome.get("tables", []), ensure_ascii=False),
                    json.dumps(outcome.get("metrics", []), ensure_ascii=False),
                    json.dumps(outcome["ast"], ensure_ascii=False) if outcome.get("ast") is not None else None,
                    outcome.get("sql"), outcome.get("error"), outcome.get("clarification"),
                    outcome.get("attempts", 0), outcome.get("total_tokens", 0), int(elapsed_ms),
                ))
                cur.executemany("""
                    INSERT INTO ask_trace (ask_id, stage, candidates, selected, reason, prompt_tokens,
                                           completion_tokens, elapsed_ms, model)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, [(str(entry_id), s["stage"], json.dumps(s["candidates"], ensure_ascii=False),
                       json.dumps(s["selected"], ensure_ascii=False), s["reason"], s["prompt_tokens"],
                       s["completion_tokens"], s["elapsed_ms"], s["model"]) for s in outcome.get("steps", [])])
    except Exception:
        log.exception("질문 기록 실패: %r", question)
        return None
    return entry_id


def list_history(system_id: UUID | None = None, favorite: bool | None = None,
                 feedback: str | None = None, limit: int = 50, offset: int = 0) -> list[HistoryEntry]:
    rows = db.query("""
        SELECT a.*, s.name AS system_name
          FROM ask_log a JOIN meta_system s ON s.id = a.system_id
         WHERE (%s::uuid IS NULL OR a.system_id = %s::uuid)
           AND (%s::boolean IS NULL OR a.favorite = %s::boolean)
           AND (%s::text IS NULL OR a.feedback = %s::text)
         ORDER BY a.created_at DESC
         LIMIT %s OFFSET %s
    """, *(2 * [str(system_id) if system_id else None]), *(2 * [favorite]),
        *(2 * [feedback]), min(max(limit, 1), 500), max(offset, 0))
    return [_row_to_entry(r) for r in rows]


def get_history(entry_id: UUID) -> HistoryEntry:
    row = db.one("""
        SELECT a.*, s.name AS system_name FROM ask_log a JOIN meta_system s ON s.id = a.system_id WHERE a.id = %s
    """, str(entry_id))
    if row is None:
        raise ApiException(404, "질문 기록을 찾을 수 없습니다")
    return _row_to_entry(row)


def get_trace(entry_id: UUID) -> list[AskStep]:
    """질문 하나의 단계별 기록 — 어느 단계에서 틀렸는지 가르는 근거"""
    get_history(entry_id)
    rows = db.query("SELECT * FROM ask_trace WHERE ask_id = %s ORDER BY id", str(entry_id))
    return [AskStep(stage=r["stage"], candidates=r["candidates"], selected=r["selected"], reason=r["reason"],
                    prompt_tokens=r["prompt_tokens"], completion_tokens=r["completion_tokens"],
                    elapsed_ms=r["elapsed_ms"]) for r in rows]


def patch_history(entry_id: UUID, patch: HistoryPatch) -> HistoryEntry:
    current = get_history(entry_id)
    favorite = current.favorite if patch.favorite is None else patch.favorite
    feedback = current.feedback if patch.feedback is None else (patch.feedback or None)
    note = current.feedback_note if patch.feedback_note is None else patch.feedback_note.strip()
    if feedback == "up" and not current.sql:
        raise ApiException(400, "SQL 이 없는 기록은 '맞음'으로 표시할 수 없습니다")
    with db.connection() as conn:
        conn.execute("UPDATE ask_log SET favorite = %s, feedback = %s, feedback_note = %s WHERE id = %s",
                     (favorite, feedback, note, str(entry_id)))
    return get_history(entry_id)


def delete_history(entry_id: UUID) -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM ask_log WHERE id = %s", (str(entry_id),))


def export_eval_cases(system_id: UUID) -> dict:
    """'맞음' 피드백을 받은 기록을 평가셋 파일 형식으로. evaluate.py --cases 가 그대로 읽는다."""
    system = db.one("SELECT name FROM meta_system WHERE id = %s", str(system_id))
    if system is None:
        raise ApiException(404, "시스템을 찾을 수 없습니다")
    rows = db.query("""
        SELECT DISTINCT ON (lower(question)) question, ast, sql, feedback_note, created_at
          FROM ask_log
         WHERE system_id = %s AND feedback = 'up' AND sql IS NOT NULL
         ORDER BY lower(question), created_at DESC
    """, str(system_id))
    return {
        "system": system["name"],
        "cases": [
            {"question": r["question"], "ast": r["ast"], "expected_sql": r["sql"],
             **({"note": r["feedback_note"]} if r["feedback_note"] else {})}
            for r in rows
        ],
    }


def promote_to_eval_case(entry_id: UUID) -> int:
    """'맞음' 기록 하나를 평가 문항(eval_case, origin=human, approved)으로 옮긴다.

    정답 테이블·지표는 그 질문이 실제로 고른 것 — 사람이 '맞음' 이라고 확인했기 때문이다.
    """
    row = db.one("SELECT * FROM ask_log WHERE id = %s", str(entry_id))
    if row is None:
        raise ApiException(404, "질문 기록을 찾을 수 없습니다")
    if row["feedback"] != "up" or not row["sql"]:
        raise ApiException(400, "'맞음' 으로 표시된 기록만 평가 문항으로 옮길 수 있습니다")
    tables = db.query("SELECT id FROM meta_table WHERE system_id = %s AND name = ANY(%s)",
                      str(row["system_id"]), row["selected_tables"] or [])
    metric = db.one("SELECT id FROM metric WHERE system_id = %s AND name = %s",
                    str(row["system_id"]), (row["selected_metrics"] or [None])[0])
    with db.connection() as conn:
        cur = conn.execute("""
            INSERT INTO eval_case (system_id, question, expected_tables, expected_metric_id, expected_sql,
                                   origin, style, status)
            VALUES (%s, %s, %s, %s, %s, 'human', '', 'approved') RETURNING id
        """, (str(row["system_id"]), row["question"], json.dumps([str(t["id"]) for t in tables]),
              str(metric["id"]) if metric else None, row["sql"]))
        return cur.fetchone()[0]


def _row_to_entry(row: dict) -> HistoryEntry:
    return HistoryEntry(
        id=row["id"], system_id=row["system_id"], system_name=row["system_name"],
        question=row["question"], selected_tables=row["selected_tables"] or [],
        selected_metrics=row["selected_metrics"] or [], total_tokens=row["total_tokens"],
        sql=row["sql"], ast=row["ast"], error=row["error"],
        clarification=row["clarification"], attempts=row["attempts"], elapsed_ms=row["elapsed_ms"],
        favorite=row["favorite"], feedback=row["feedback"], feedback_note=row["feedback_note"],
        created_at=row["created_at"].isoformat(),
    )
