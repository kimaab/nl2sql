import json
import logging
from uuid import UUID, uuid4

import db
from models import ApiException, HistoryEntry, HistoryPatch

log = logging.getLogger("nl2sql.history")


def record_ask(datasource_id: UUID, question: str, outcome: dict, elapsed_ms: float) -> UUID | None:
    """질문 결과를 기록한다. 기록이 실패해도 답은 돌려준다 — 기록은 부가 기능이다."""
    entry_id = uuid4()
    try:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ask_log
                    (id, datasource_id, question, sql, ast, error, clarification, attempts, elapsed_ms)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    str(entry_id), str(datasource_id), question, outcome.get("sql"),
                    json.dumps(outcome["ast"], ensure_ascii=False) if outcome.get("ast") is not None else None,
                    outcome.get("error"), outcome.get("clarification"), outcome.get("attempts", 0),
                    int(elapsed_ms),
                ))
    except Exception:
        log.exception("질문 기록 실패: %r", question)
        return None
    return entry_id


def list_history(datasource_id: UUID | None = None, favorite: bool | None = None,
                 feedback: str | None = None, limit: int = 50, offset: int = 0) -> list[HistoryEntry]:
    rows = db.query("""
        SELECT a.*, d.name AS datasource_name
          FROM ask_log a JOIN datasource d ON d.id = a.datasource_id
         WHERE (%s::uuid IS NULL OR a.datasource_id = %s::uuid)
           AND (%s::boolean IS NULL OR a.favorite = %s::boolean)
           AND (%s::text IS NULL OR a.feedback = %s::text)
         ORDER BY a.created_at DESC
         LIMIT %s OFFSET %s
    """, *(2 * [str(datasource_id) if datasource_id else None]), *(2 * [favorite]),
        *(2 * [feedback]), min(max(limit, 1), 500), max(offset, 0))
    return [_row_to_entry(r) for r in rows]


def get_history(entry_id: UUID) -> HistoryEntry:
    row = db.one("""
        SELECT a.*, d.name AS datasource_name
          FROM ask_log a JOIN datasource d ON d.id = a.datasource_id WHERE a.id = %s
    """, str(entry_id))
    if row is None:
        raise ApiException(404, "질문 기록을 찾을 수 없습니다")
    return _row_to_entry(row)


def patch_history(entry_id: UUID, patch: HistoryPatch) -> HistoryEntry:
    current = get_history(entry_id)
    favorite = current.favorite if patch.favorite is None else patch.favorite
    feedback = current.feedback if patch.feedback is None else (patch.feedback or None)
    note = current.feedback_note if patch.feedback_note is None else patch.feedback_note.strip()
    if feedback == "up" and not current.sql:
        raise ApiException(400, "SQL 이 없는 기록은 '맞음'으로 표시할 수 없습니다")
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ask_log SET favorite = %s, feedback = %s, feedback_note = %s WHERE id = %s",
                (favorite, feedback, note, str(entry_id)),
            )
    return get_history(entry_id)


def delete_history(entry_id: UUID) -> None:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ask_log WHERE id = %s", (str(entry_id),))


def export_eval_cases(datasource_id: UUID) -> dict:
    """'맞음' 피드백을 받은 기록을 평가셋 형식으로. evaluate.py 가 그대로 읽는다."""
    ds = db.one("SELECT name FROM datasource WHERE id = %s", str(datasource_id))
    if ds is None:
        raise ApiException(404, "데이터소스를 찾을 수 없습니다")
    rows = db.query("""
        SELECT DISTINCT ON (lower(question)) question, ast, sql, feedback_note, created_at
          FROM ask_log
         WHERE datasource_id = %s AND feedback = 'up' AND sql IS NOT NULL
         ORDER BY lower(question), created_at DESC
    """, str(datasource_id))
    return {
        "datasource": ds["name"],
        "cases": [
            {"question": r["question"], "ast": r["ast"], "expected_sql": r["sql"],
             **({"note": r["feedback_note"]} if r["feedback_note"] else {})}
            for r in rows
        ],
    }


def _row_to_entry(row: dict) -> HistoryEntry:
    return HistoryEntry(
        id=row["id"], datasource_id=row["datasource_id"], datasource_name=row["datasource_name"],
        question=row["question"], sql=row["sql"], ast=row["ast"], error=row["error"],
        clarification=row["clarification"], attempts=row["attempts"], elapsed_ms=row["elapsed_ms"],
        favorite=row["favorite"], feedback=row["feedback"], feedback_note=row["feedback_note"],
        created_at=row["created_at"].isoformat(),
    )
