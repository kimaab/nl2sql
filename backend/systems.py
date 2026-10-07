import hashlib
import json
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

import db
import secret
from catalog import read_catalog
from models import ApiException, Driver, SyncLog, SyncResult, System, SystemInput, TableInfo
from relations import replace_fk_relations

log = logging.getLogger("nl2sql.systems")

_COUNTS = """
    (SELECT count(*) FROM meta_table t WHERE t.system_id = s.id AND t.deleted_at IS NULL) AS table_count,
    (SELECT count(*) FROM metric m WHERE m.system_id = s.id) AS metric_count,
    (SELECT count(*) FROM metric m WHERE m.system_id = s.id AND m.status = 'active') AS active_metric_count
"""


def _guard_duplicate(data: SystemInput, except_id: UUID | None = None) -> None:
    row = db.one(
        "SELECT code, name FROM meta_system WHERE (lower(name) = lower(%s) OR code = %s) AND id <> %s",
        data.name, data.code, str(except_id or uuid4()),
    )
    if row:
        what = "코드" if row["code"] == data.code else "이름"
        raise ApiException(409, f"같은 {what}의 시스템이 이미 있습니다")


def list_systems() -> list[System]:
    rows = db.query(f"SELECT s.*, {_COUNTS} FROM meta_system s ORDER BY s.name")
    return [_row_to_system(r) for r in rows]


def get_system(system_id: UUID) -> System:
    row = db.one(f"SELECT s.*, {_COUNTS} FROM meta_system s WHERE s.id = %s", str(system_id))
    if row is None:
        raise ApiException(404, "시스템을 찾을 수 없습니다")
    return _row_to_system(row)


def create_system(data: SystemInput) -> System:
    _guard_duplicate(data)
    system_id = uuid4()
    with db.connection() as conn:
        conn.execute("""
            INSERT INTO meta_system (id, code, name, domain_desc, driver, host, port, db_name, db_schema,
                                     username, password_enc)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (str(system_id), data.code, data.name, data.domain_desc, data.driver.value, data.host, data.port,
              data.db_name, data.db_schema, data.username, secret.encrypt(data.password)))
    log.info("system created: %s (%s)", system_id, data.code)
    return get_system(system_id)


def update_system(system_id: UUID, data: SystemInput) -> System:
    get_system(system_id)
    _guard_duplicate(data, except_id=system_id)
    with db.connection() as conn:
        conn.execute("""
            UPDATE meta_system SET code = %s, name = %s, domain_desc = %s, driver = %s, host = %s, port = %s,
                   db_name = %s, db_schema = %s, username = %s,
                   password_enc = COALESCE(%s, password_enc), updated_at = now()
             WHERE id = %s
        """, (data.code, data.name, data.domain_desc, data.driver.value, data.host, data.port, data.db_name,
              data.db_schema, data.username, secret.encrypt(data.password) if data.password else None,
              str(system_id)))
    log.info("system updated: %s", system_id)
    return get_system(system_id)


def delete_system(system_id: UUID) -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM meta_system WHERE id = %s", (str(system_id),))
    log.info("system deleted: %s", system_id)


def connection_row(system_id: UUID) -> dict:
    """카탈로그 읽기용 접속 정보. 비밀번호는 여기서만 복호화한다."""
    row = db.one("SELECT * FROM meta_system WHERE id = %s", str(system_id))
    if row is None:
        raise ApiException(404, "시스템을 찾을 수 없습니다")
    try:
        row["password"] = secret.decrypt(row["password_enc"])
    except RuntimeError:
        raise
    except Exception:
        raise ApiException(400, f"{row['name']} 의 비밀번호를 풀 수 없습니다 (키가 바뀌었으면 비밀번호를 다시 입력하십시오)") from None
    return row


# ── 동기화 ───────────────────────────────────────────────────────────────

def sync_system(system_id: UUID, trigger: str = "manual") -> SyncResult:
    """스키마 동기화. 대상 DB 에 접속하는 유일한 지점. 성공·실패 모두 동기화 기록에 남는다."""
    from metrics import refresh_statuses  # 순환 import 를 피한다 (metrics → contract → …)

    row = connection_row(system_id)
    started = datetime.now(timezone.utc)
    try:
        tables, relations = read_catalog(row)
        result = _merge_schema(system_id, tables, relations)
    except ApiException as error:
        _write_sync_log(system_id, trigger, started, error=error.detail)
        raise
    except Exception as error:
        _write_sync_log(system_id, trigger, started,
                        error=str(error).strip().splitlines()[0] if str(error) else type(error).__name__)
        raise

    result.broken_metrics = refresh_statuses(system_id)
    for broken in result.broken_metrics:
        log.warning("동기화 뒤 깨진 지표: %s / %s — %s", row["name"], broken["name"], broken["reason"])
    _write_sync_log(system_id, trigger, started, result=result)
    return result


def structure_hash(purpose: str, columns: list[tuple[str, str]]) -> str:
    """카드를 다시 만들어야 하는지 가르는 값: 용도 + 컬럼 구성."""
    body = json.dumps([purpose, sorted(columns)], ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _merge_schema(system_id: UUID, tables: list[dict], relations: list[dict] | None) -> SyncResult:
    """이름 기준 병합. 있으면 UPDATE(id 유지), 없으면 INSERT, 사라지면 deleted_at.

    전체를 한 트랜잭션으로 처리한다 — 질문 도중 반쯤 바뀐 스키마를 보지 않게.
    """
    sid = str(system_id)
    added, changed, column_count = [], [], 0
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, deleted_at FROM meta_table WHERE system_id = %s", (sid,))
            existing = {name: (tid, deleted) for tid, name, deleted in cur.fetchall()}
            cur.execute("""
                SELECT c.table_id, c.name, c.data_type FROM meta_column c JOIN meta_table t ON t.id = c.table_id
                 WHERE t.system_id = %s AND c.deleted_at IS NULL
            """, (sid,))
            before: dict = {}
            for tid, name, data_type in cur.fetchall():
                before.setdefault(tid, set()).add((name, data_type))

            for table in tables:
                if table["name"] in existing:
                    tid, deleted = existing[table["name"]]
                    cur.execute("""
                        UPDATE meta_table SET comment = %s, deleted_at = NULL, synced_at = now(), updated_at = now()
                         WHERE id = %s
                    """, (table["description"], tid))
                    now_cols = {(c["name"], c["type"]) for c in table["columns"]}
                    if deleted is not None:
                        added.append(tid)
                    elif before.get(tid, set()) != now_cols:
                        changed.append(tid)
                else:
                    tid = uuid4()
                    cur.execute("INSERT INTO meta_table (id, system_id, name, comment) VALUES (%s, %s, %s, %s)",
                                (str(tid), sid, table["name"], table["description"]))
                    added.append(tid)

                names = [c["name"] for c in table["columns"]]
                if table["columns"]:
                    cur.executemany("""
                        INSERT INTO meta_column (table_id, name, data_type, comment, ordinal, is_pk)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (table_id, name) DO UPDATE SET
                            data_type = EXCLUDED.data_type, comment = EXCLUDED.comment, ordinal = EXCLUDED.ordinal,
                            is_pk = EXCLUDED.is_pk, deleted_at = NULL, updated_at = now()
                    """, [(str(tid), c["name"], c["type"], c["description"], i, bool(c.get("is_pk")))
                          for i, c in enumerate(table["columns"])])
                cur.execute("""
                    UPDATE meta_column SET deleted_at = now(), updated_at = now()
                     WHERE table_id = %s AND deleted_at IS NULL AND NOT (name = ANY(%s))
                """, (str(tid), names))
                column_count += len(names)

            cur.execute("""
                UPDATE meta_table SET deleted_at = now(), updated_at = now()
                 WHERE system_id = %s AND deleted_at IS NULL AND NOT (name = ANY(%s))
                RETURNING id
            """, (sid, [t["name"] for t in tables]))
            removed = len(cur.fetchall())

            # 새로 생기거나 구조가 바뀐 테이블은 카드를 (다시) 만든다
            if added or changed:
                cur.executemany("""
                    INSERT INTO enrich_job (target_type, target_id, reason) VALUES ('table', %s, %s)
                    ON CONFLICT (target_type, target_id) WHERE status = 'queued' DO NOTHING
                """, [(str(t), "new") for t in added] + [(str(t), "changed") for t in changed])

            if relations is not None:
                replace_fk_relations(cur, system_id, relations)
            cur.execute("UPDATE meta_system SET synced_at = now(), updated_at = now() WHERE id = %s RETURNING synced_at",
                        (sid,))
            synced = cur.fetchone()

    log.info("system %s synced: %d tables (+%d ~%d -%d), %d columns",
             system_id, len(tables), len(added), len(changed), removed, column_count)
    return SyncResult(
        table_count=len(tables), column_count=column_count,
        tables_added=len(added), tables_changed=len(changed), tables_removed=removed,
        synced_at=synced[0].isoformat(),
        relation_count=len(relations) if relations is not None else None,
    )


def _write_sync_log(system_id: UUID, trigger: str, started, result: SyncResult | None = None,
                    error: str | None = None) -> None:
    with db.connection() as conn:
        conn.execute("""
            INSERT INTO meta_sync_log (system_id, trigger, status, tables_added, tables_changed, tables_removed,
                                       broken_metrics, error, started_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (str(system_id), trigger, "error" if error else "ok",
              result.tables_added if result else None, result.tables_changed if result else None,
              result.tables_removed if result else None,
              json.dumps(result.broken_metrics if result else [], ensure_ascii=False), error, started))


def list_sync_logs(system_id: UUID | None = None, limit: int = 100) -> list[SyncLog]:
    rows = db.query("""
        SELECT l.*, s.name AS system_name
          FROM meta_sync_log l JOIN meta_system s ON s.id = l.system_id
         WHERE (%s::uuid IS NULL OR l.system_id = %s::uuid)
         ORDER BY l.started_at DESC LIMIT %s
    """, str(system_id) if system_id else None, str(system_id) if system_id else None, limit)
    return [
        SyncLog(id=r["id"], system_id=r["system_id"], system_name=r["system_name"], trigger=r["trigger"],
                status=r["status"], tables_added=r["tables_added"], tables_changed=r["tables_changed"],
                tables_removed=r["tables_removed"], broken_metrics=r["broken_metrics"] or [], error=r["error"],
                started_at=r["started_at"].isoformat(), finished_at=r["finished_at"].isoformat())
        for r in rows
    ]


# ── 테이블 정보 ─────────────────────────────────────────────────────────

def get_schema(system_id: UUID) -> dict:
    """저장된 스키마 (삭제 표시된 테이블·컬럼 제외)"""
    rows = db.query("""
        SELECT t.name AS table_name, t.comment AS table_note, t.purpose,
               c.name AS column_name, c.data_type, c.comment AS column_note
          FROM meta_table t
          LEFT JOIN meta_column c ON c.table_id = t.id AND c.deleted_at IS NULL
         WHERE t.system_id = %s AND t.deleted_at IS NULL
         ORDER BY t.name, c.ordinal
    """, str(system_id))
    tables: dict = {}
    for r in rows:
        table = tables.setdefault(r["table_name"], {"name": r["table_name"], "description": r["table_note"],
                                                    "purpose": r["purpose"], "columns": []})
        if r["column_name"] is not None:
            table["columns"].append({"name": r["column_name"], "type": r["data_type"], "description": r["column_note"]})
    return {"tables": list(tables.values())}


def list_tables(system_id: UUID) -> list[TableInfo]:
    rows = db.query("""
        SELECT t.*,
               (SELECT count(*) FROM meta_column c WHERE c.table_id = t.id AND c.deleted_at IS NULL) AS column_count,
               (SELECT count(*) FROM metric_table mt WHERE mt.table_id = t.id) AS metric_count
          FROM meta_table t WHERE t.system_id = %s AND t.deleted_at IS NULL ORDER BY t.name
    """, str(system_id))
    return [TableInfo(id=r["id"], name=r["name"], comment=r["comment"], purpose=r["purpose"], card=r["card"],
                      card_line=r["card_line"], card_status=r["card_status"], column_count=r["column_count"],
                      metric_count=r["metric_count"]) for r in rows]


def set_table_purpose(system_id: UUID, table_id: UUID, purpose: str) -> TableInfo:
    """용도가 바뀌면 카드를 다시 만든다 (보강 대기열)."""
    with db.connection() as conn:
        cur = conn.execute("""
            UPDATE meta_table SET purpose = %s, updated_at = now()
             WHERE id = %s AND system_id = %s AND deleted_at IS NULL AND purpose IS DISTINCT FROM %s
            RETURNING id
        """, (purpose.strip(), str(table_id), str(system_id), purpose.strip()))
        if cur.fetchone():
            conn.execute("""
                INSERT INTO enrich_job (target_type, target_id, reason) VALUES ('table', %s, 'changed')
                ON CONFLICT (target_type, target_id) WHERE status = 'queued' DO NOTHING
            """, (str(table_id),))
    found = next((t for t in list_tables(system_id) if t.id == table_id), None)
    if found is None:
        raise ApiException(404, "테이블을 찾을 수 없습니다")
    return found


def _row_to_system(row: dict) -> System:
    return System(
        id=row["id"], code=row["code"], name=row["name"], domain_desc=row["domain_desc"],
        driver=Driver(row["driver"]), host=row["host"], port=row["port"], db_name=row["db_name"],
        db_schema=row["db_schema"], username=row["username"],
        synced_at=row["synced_at"].isoformat() if row["synced_at"] else None,
        table_count=row["table_count"], metric_count=row["metric_count"],
        active_metric_count=row["active_metric_count"],
    )
