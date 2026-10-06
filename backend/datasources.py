import json
import logging
from uuid import UUID, uuid4
from datetime import datetime, timezone
import db
from models import Datasource, DatasourceInput, ApiException, SyncResult, SyncLog, Driver
from catalog import read_catalog
from contract import broken_metric_list
from relations import replace_fk_relations

log = logging.getLogger("nl2sql.datasources")


def _guard_duplicate_name(name: str, except_id: UUID | None = None) -> None:
    """중복된 이름 검사"""
    if except_id is None:
        row = db.one("SELECT id FROM datasource WHERE lower(name) = lower(%s)", name)
    else:
        row = db.one(
            "SELECT id FROM datasource WHERE lower(name) = lower(%s) AND id <> %s",
            name,
            str(except_id)
        )
    if row:
        raise ApiException(409, f"'{name}' 이름의 데이터소스가 이미 있습니다")


def list_datasources() -> list[Datasource]:
    """데이터소스 목록"""
    rows = db.query("""
        SELECT d.*,
               (SELECT count(*) FROM datasource_table t WHERE t.datasource_id = d.id) AS table_count,
               (SELECT count(*) FROM datasource_metric m WHERE m.datasource_id = d.id) AS metric_count
          FROM datasource d
         ORDER BY d.name
    """)
    return [_row_to_datasource(r) for r in rows]


def get_datasource(datasource_id: UUID) -> Datasource:
    """데이터소스 조회"""
    row = db.one(
        """
        SELECT d.*,
               (SELECT count(*) FROM datasource_table t WHERE t.datasource_id = d.id) AS table_count,
               (SELECT count(*) FROM datasource_metric m WHERE m.datasource_id = d.id) AS metric_count
          FROM datasource d
         WHERE d.id = %s
        """,
        str(datasource_id)
    )
    if row is None:
        raise ApiException(404, "데이터소스를 찾을 수 없습니다")
    return _row_to_datasource(row)


def create_datasource(input_data: DatasourceInput) -> Datasource:
    """데이터소스 생성"""
    _guard_duplicate_name(input_data.name)

    datasource_id = uuid4()
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO datasource (id, name, description, driver, host, port, db_name, db_schema, username, password)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                str(datasource_id),
                input_data.name,
                input_data.description,
                input_data.driver.value,
                input_data.host,
                input_data.port,
                input_data.db_name,
                input_data.db_schema,
                input_data.username,
                input_data.password,
            ))
        conn.commit()

    log.info("datasource created: %s (%s)", datasource_id, input_data.name)
    return get_datasource(datasource_id)


def update_datasource(datasource_id: UUID, input_data: DatasourceInput) -> Datasource:
    """데이터소스 수정"""
    _guard_duplicate_name(input_data.name, except_id=datasource_id)

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE datasource SET
                    name = %s, description = %s, driver = %s, host = %s, port = %s,
                    db_name = %s, db_schema = %s, username = %s,
                    password = CASE WHEN %s = '' THEN password ELSE %s END,
                    updated_at = now()
                WHERE id = %s
            """, (
                input_data.name,
                input_data.description,
                input_data.driver.value,
                input_data.host,
                input_data.port,
                input_data.db_name,
                input_data.db_schema,
                input_data.username,
                input_data.password,
                input_data.password,
                str(datasource_id),
            ))
        conn.commit()

    log.info("datasource updated: %s", datasource_id)
    return get_datasource(datasource_id)


def delete_datasource(datasource_id: UUID) -> None:
    """데이터소스 삭제"""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM datasource WHERE id = %s", (str(datasource_id),))
        conn.commit()

    log.info("datasource deleted: %s", datasource_id)


def sync_datasource(datasource_id: UUID, trigger: str = "manual") -> SyncResult:
    """스키마 동기화. 성공·실패 모두 동기화 기록에 남고, 깨진 지표가 생기면 경고한다."""
    row = db.one("SELECT * FROM datasource WHERE id = %s", str(datasource_id))
    if row is None:
        raise ApiException(404, "데이터소스를 찾을 수 없습니다")

    started = datetime.now(timezone.utc)
    try:
        # 대상 DB에서 스키마 읽기
        tables, relations = read_catalog(row)
        # 메타데이터 DB에 저장
        result = _replace_schema(datasource_id, tables, relations)
    except ApiException as error:
        _write_sync_log(datasource_id, trigger, started, error=error.detail)
        raise
    except Exception as error:
        _write_sync_log(datasource_id, trigger, started, error=str(error).strip().splitlines()[0] if str(error) else type(error).__name__)
        raise

    result.broken_metrics = broken_metric_list(datasource_id)
    for broken in result.broken_metrics:
        log.warning("동기화 뒤 깨진 지표: %s / %s — %s", row["name"], broken["name"], broken["reason"])
    _write_sync_log(datasource_id, trigger, started, result=result)
    return result


def _write_sync_log(datasource_id: UUID, trigger: str, started, result: SyncResult | None = None,
                    error: str | None = None) -> None:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO datasource_sync_log
                (datasource_id, trigger, status, table_count, column_count, relation_count, broken_metrics, error, started_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                str(datasource_id), trigger, "error" if error else "ok",
                result.table_count if result else None,
                result.column_count if result else None,
                result.relation_count if result else None,
                json.dumps(result.broken_metrics if result else [], ensure_ascii=False),
                error, started,
            ))


def list_sync_logs(datasource_id: UUID | None = None, limit: int = 100) -> list[SyncLog]:
    rows = db.query("""
        SELECT l.*, d.name AS datasource_name
          FROM datasource_sync_log l JOIN datasource d ON d.id = l.datasource_id
         WHERE (%s::uuid IS NULL OR l.datasource_id = %s::uuid)
         ORDER BY l.started_at DESC LIMIT %s
    """, str(datasource_id) if datasource_id else None, str(datasource_id) if datasource_id else None, limit)
    return [
        SyncLog(
            id=r["id"], datasource_id=r["datasource_id"], datasource_name=r["datasource_name"],
            trigger=r["trigger"], status=r["status"], table_count=r["table_count"],
            column_count=r["column_count"], relation_count=r["relation_count"],
            broken_metrics=r["broken_metrics"] or [], error=r["error"],
            started_at=r["started_at"].isoformat(), finished_at=r["finished_at"].isoformat(),
        )
        for r in rows
    ]


def _replace_schema(datasource_id: UUID, tables: list[dict], relations: list[dict] | None = None) -> SyncResult:
    """스키마를 통째로 교체"""
    table_rows, column_rows = [], []
    for table in tables:
        table_id = uuid4()
        table_rows.append((str(table_id), str(datasource_id), table["name"], table["description"]))
        for ordinal, column in enumerate(table["columns"]):
            column_rows.append((
                str(table_id),
                column["name"],
                column["type"],
                column["description"],
                ordinal
            ))

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM datasource_table WHERE datasource_id = %s", (str(datasource_id),))
            if table_rows:
                cur.executemany(
                    "INSERT INTO datasource_table (id, datasource_id, name, description) VALUES (%s,%s,%s,%s)",
                    table_rows
                )
            if column_rows:
                cur.executemany(
                    "INSERT INTO datasource_column (table_id, name, data_type, description, ordinal) VALUES (%s,%s,%s,%s,%s)",
                    column_rows
                )
            cur.execute(
                "UPDATE datasource SET synced_at = now(), updated_at = now() WHERE id = %s RETURNING synced_at",
                (str(datasource_id),)
            )
            synced = cur.fetchone()
            # 관계도 같은 트랜잭션에서 교체한다. None 이면 FK 를 읽지 못한 것이라 그대로 둔다.
            if relations is not None:
                replace_fk_relations(cur, datasource_id, relations)
        conn.commit()

    log.info("datasource %s synced: %d tables, %d columns", datasource_id, len(tables), len(column_rows))
    return SyncResult(
        table_count=len(tables),
        column_count=len(column_rows),
        synced_at=synced[0].isoformat() if synced else datetime.now().isoformat(),
        relation_count=len(relations) if relations is not None else None,
    )


def get_schema(datasource_id: UUID) -> dict:
    """저장된 스키마 조회"""
    rows = db.query("""
        SELECT t.name AS table_name, t.description AS table_note,
               c.name AS column_name, c.data_type, c.description AS column_note
          FROM datasource_table t
          LEFT JOIN datasource_column c ON c.table_id = t.id
         WHERE t.datasource_id = %s
         ORDER BY t.name, c.ordinal
    """, str(datasource_id))

    tables = {}
    for r in rows:
        if r["table_name"] not in tables:
            tables[r["table_name"]] = {
                "name": r["table_name"],
                "description": r["table_note"],
                "columns": []
            }
        if r["column_name"] is not None:
            tables[r["table_name"]]["columns"].append({
                "name": r["column_name"],
                "type": r["data_type"],
                "description": r["column_note"]
            })

    return {"tables": list(tables.values())}


def _row_to_datasource(row: dict) -> Datasource:
    """DB 행을 Datasource 모델로 변환"""
    return Datasource(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        driver=Driver(row["driver"]),
        host=row["host"],
        port=row["port"],
        db_name=row["db_name"],
        db_schema=row["db_schema"],
        username=row["username"],
        synced_at=row["synced_at"].isoformat() if row["synced_at"] else None,
        table_count=row["table_count"],
        metric_count=row["metric_count"],
    )
