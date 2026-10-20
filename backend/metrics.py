"""지표 = 이름 · 설명 · 같은 말 + SQL. SQL 을 AST 로 옮겨 저장하고, 질문마다 LLM 이 그 AST 의 조건만 바꿔 쓴다.

등록할 때 AST 를 다시 컴파일한 SQL 이 원본과 같은 결과를 내는지 대상 DB 에서 확인한다 — AST 로 못 옮기는 문법은
변환에서 걸리고, 옮기며 뜻이 바뀌면(정수 나눗셈 등) 결과 비교에서 걸린다.
"""

import json
import logging
from contextlib import contextmanager
from uuid import UUID, uuid4

import oracledb
import psycopg
import pymysql
from psycopg.conninfo import make_conninfo

import db
from compiler import Compiler, ast_tables
from contract import load_contract, load_metric_rows, load_tables
from models import ApiException, Metric, MetricCheck, MetricHistoryEntry, MetricInput
from sql_ast import parse_sql

log = logging.getLogger("nl2sql.metrics")

VERIFY_TIMEOUT_S = 60
MAX_COMPARE_ROWS = 100_000  # 해시로 비교하지 못하는 드라이버(MySQL·Oracle)에서 가져와 비교할 최대 행 수
STAGE_LABEL = {"parse": "AST 변환 실패", "compile": "컴파일 실패", "guard": "실행 전 확인에서 멈춤", "verify": "결과 비교 실패"}


def list_metrics(system_id: UUID) -> list[Metric]:
    rows = load_metric_rows(system_id)
    compiler = _compiler(system_id)
    metrics = []
    for r in sorted(rows, key=lambda r: r["created_at"], reverse=True):
        metrics.append(_row_to_metric(r, compiler))
    return metrics


def get_metric(system_id: UUID, metric_id: UUID) -> Metric:
    for row in load_metric_rows(system_id):
        if str(row["id"]) == str(metric_id):
            return _row_to_metric(row, _compiler(system_id))
    raise ApiException(404, "지표를 찾을 수 없습니다")


# ── 등록 전 확인: SQL → AST → SQL → 결과 비교 ──────────────────────────────


def check_metric(system_id: UUID, sql: str, guard=None) -> MetricCheck:
    """저장하지 않고 등록 절차만 밟아 본다. 실패하면 error 와 단계(stage)를 채워 돌려준다.

    guard(원본 SQL, 옮긴 SQL) 는 대상 DB 에서 실행하기 전에 부른다 — 실행하면 안 되는 이유를 돌려주면 실행하지 않는다.
    """
    contract = load_contract(system_id)
    try:
        parsed = parse_sql(sql, contract)
    except ValueError as error:
        return MetricCheck(stage="parse", error=str(error))
    ast, warnings = parsed["ast"], parsed["warnings"]
    try:
        compiled = Compiler(contract).compile(ast)
    except ValueError as error:
        return MetricCheck(ast=ast, warnings=warnings, stage="compile", error=str(error))
    out = MetricCheck(ast=ast, sql=compiled, tables=ast_tables(ast), warnings=warnings)
    if guard is not None:
        reason = guard(sql, compiled)
        if reason:
            out.stage, out.error = "guard", reason
            return out
    try:
        out.rows = verify(system_id, sql, compiled)
    except ValueError as error:
        out.stage, out.error = "verify", str(error)
    except ApiException as error:
        out.stage, out.error = "verify", str(error.detail)
    return out


def verify(system_id: UUID, original: str, compiled: str) -> int:
    """원본 SQL 과 AST 에서 다시 만든 SQL 을 대상 DB 에서 실행해 결과가 같은지 본다 → 행 수.

    한 읽기 전용 트랜잭션의 같은 스냅샷에서 돌린다 — 데이터가 계속 쌓이는 테이블이어도 두 쿼리가 같은 데이터를 본다.
    행 순서는 보지 않는다 (ORDER BY 없는 결과는 순서가 정해지지 않는다).
    """
    from systems import connection_row  # 순환 import 를 피한다 (systems → metrics)

    row = connection_row(system_id)
    original = str(original).strip().rstrip(";")
    with _snapshot(row) as digest:
        a, b = digest(original, "원본"), digest(compiled, "옮긴")
        if a != b and digest(original, "원본") != a:
            raise ValueError(
                "원본 SQL 의 결과가 실행할 때마다 달라 비교할 수 없습니다. "
                "ORDER BY 기준이 같은 행이 여럿이면(예: 같은 시각에 두 건) 어느 행이 뽑힐지 정해지지 않습니다 — "
                "ORDER BY 에 기준 컬럼을 더해 한 행으로 정해지게 하십시오."
            )
    if a != b:
        raise ValueError(
            f"원본과 AST 로 옮긴 SQL 의 결과가 다릅니다 (원본 {a[0]}행, 옮긴 것 {b[0]}행). "
            "옮기며 뜻이 바뀌었습니다 — 예: 정수끼리 나누기(/)는 소수 나누기가 됩니다."
        )
    return a[0]


@contextmanager
def read_only(row: dict, timeout_s: int = VERIFY_TIMEOUT_S):
    """대상 DB 의 읽기 전용 트랜잭션 커서를 준다. 쿼리 하나가 timeout_s 를 넘으면 끊는다.

    PostgreSQL 은 REPEATABLE READ, MySQL 은 CONSISTENT SNAPSHOT 이라 트랜잭션 안의 쿼리가 같은 스냅샷을 본다.
    """
    driver = row["driver"]
    try:
        if driver == "postgresql":
            conn = psycopg.connect(make_conninfo(
                host=row["host"], port=row["port"], user=row["username"] or None,
                password=row["password"] or None, dbname=row["db_name"], connect_timeout=10,
            ))
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
        elif driver == "mysql":
            conn = pymysql.connect(
                host=row["host"], port=row["port"], user=row["username"], password=row["password"],
                database=row["db_name"], connect_timeout=10, read_timeout=timeout_s, charset="utf8mb4",
            )
        else:
            conn = oracledb.connect(
                user=row["username"], password=row["password"], host=row["host"], port=row["port"],
                service_name=row["db_name"], tcp_connect_timeout=10,
            )
            conn.call_timeout = timeout_s * 1000
    except Exception as error:
        raise ApiException(502, f"{row['name']} 에 접속하지 못했습니다: {_first_line(error)}") from error

    cur = conn.cursor()
    try:
        if driver == "postgresql":
            cur.execute(f"SET LOCAL statement_timeout = '{int(timeout_s)}s'")
            if row.get("db_schema"):
                cur.execute("SELECT set_config('search_path', %s, true)", (row["db_schema"],))
        elif driver == "mysql":
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        else:
            cur.execute("SET TRANSACTION READ ONLY")
    except Exception as error:
        conn.close()
        raise ApiException(502, f"{row['name']} 에서 읽기 전용 트랜잭션을 열지 못했습니다: {_first_line(error)}") from error

    try:
        yield cur
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


@contextmanager
def _snapshot(row: dict):
    """같은 스냅샷에서 SQL 의 비교값을 만드는 함수 digest(sql, 이름) 를 준다.

    PostgreSQL: (행 수, 결과 전체의 md5) 를 DB 안에서 만든다.
    MySQL · Oracle: 행을 가져와 (행 수, 정렬한 행 목록) 으로 비교한다.
    """
    driver = row["driver"]

    with read_only(row) as cur:

        def digest(sql: str, which: str) -> tuple:
            try:
                if driver == "postgresql":
                    cur.execute(
                        "SELECT count(*), md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), '')) "
                        f"FROM ({sql}) t"
                    )
                    return tuple(cur.fetchone())
                cur.execute(sql)
                rows = cur.fetchmany(MAX_COMPARE_ROWS + 1)
                if len(rows) > MAX_COMPARE_ROWS:
                    raise ValueError(f"결과가 {MAX_COMPARE_ROWS:,}행을 넘어 비교할 수 없습니다 — 기간을 좁혀 등록하십시오")
                printed = []
                for r in rows:
                    printed.append(repr(tuple(r)))
                return len(rows), sorted(printed)
            except ValueError:
                raise
            except psycopg.errors.QueryCanceled:
                raise ValueError(f"결과 비교가 {VERIFY_TIMEOUT_S}초를 넘었습니다 — 기간을 좁혀 등록하십시오") from None
            except Exception as error:
                raise ValueError(f"{which} SQL 실행 실패: {_first_line(error)}") from None

        yield digest


def _first_line(error) -> str:
    lines = str(error).strip().splitlines()
    return lines[0] if lines else type(error).__name__


# ── 저장 ─────────────────────────────────────────────────────────────────


def create_metric(system_id: UUID, data: MetricInput) -> Metric:
    _guard_duplicate_name(system_id, data.name)
    check = _checked(system_id, data.sql)
    metric_id = uuid4()
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO metric (id, system_id, name, description, synonyms, source_sql, ast)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
                (str(metric_id), str(system_id), *_values(data, check)),
            )
            _save_tables(cur, system_id, metric_id, check.tables)
            _record_history(cur, system_id, metric_id, 1, "create")
    log.info("metric created: %s (%s)", metric_id, data.name)
    return get_metric(system_id, metric_id)


def update_metric(system_id: UUID, metric_id: UUID, data: MetricInput) -> Metric:
    """지표 수정. 버전을 올리고 이전 정의는 이력에 남는다."""
    current = get_metric(system_id, metric_id)
    _guard_duplicate_name(system_id, data.name, except_id=metric_id)
    check = _checked(system_id, data.sql)
    version = current.version + 1
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE metric SET name = %s, description = %s, synonyms = %s, source_sql = %s, ast = %s,
                       status = 'active', broken_reason = NULL, version = %s, updated_at = now()
                 WHERE id = %s AND system_id = %s
            """,
                (*_values(data, check), version, str(metric_id), str(system_id)),
            )
            _save_tables(cur, system_id, metric_id, check.tables)
            _record_history(cur, system_id, metric_id, version, "update")
    log.info("metric updated: %s (%s, v%d)", metric_id, data.name, version)
    return get_metric(system_id, metric_id)


def delete_metric(system_id: UUID, metric_id: UUID) -> None:
    """지표 삭제. 마지막 정의는 이력에 남는다."""
    row = db.one(
        "SELECT name, version FROM metric WHERE id = %s AND system_id = %s",
        str(metric_id),
        str(system_id),
    )
    if row is None:
        return
    with db.connection() as conn:
        with conn.cursor() as cur:
            _record_history(cur, system_id, metric_id, row["version"], "delete")
            cur.execute(
                "DELETE FROM metric WHERE id = %s AND system_id = %s",
                (str(metric_id), str(system_id)),
            )
    log.info("metric deleted: %s", metric_id)


def list_metric_history(system_id: UUID, metric_id: UUID) -> list[MetricHistoryEntry]:
    rows = db.query(
        """
        SELECT version, action, snapshot, created_at FROM metric_history
         WHERE system_id = %s AND metric_id = %s ORDER BY id DESC
    """,
        str(system_id),
        str(metric_id),
    )
    entries = []
    for r in rows:
        entries.append(
            MetricHistoryEntry(
                version=r["version"],
                action=r["action"],
                snapshot=r["snapshot"],
                created_at=r["created_at"].isoformat(),
            )
        )
    return entries


def refresh_statuses(system_id: UUID) -> list[dict]:
    """동기화 뒤 지표 상태를 다시 매긴다: AST 가 지금 스키마로 컴파일되지 않으면 broken.

    깨진 지표 [{name, reason}] 를 돌려준다.
    """
    compiler = _compiler(system_id)
    broken, updates = [], []
    for r in load_metric_rows(system_id):
        try:
            compiler.compile(r["ast"])
            status, reason = "active", None
        except ValueError as error:
            status, reason = "broken", f"스키마와 맞지 않습니다 — {error}"
            broken.append({"name": r["name"], "reason": reason})
        if status != r["status"] or reason != r["broken_reason"]:
            updates.append((status, reason, str(r["id"])))
    if updates:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany("UPDATE metric SET status = %s, broken_reason = %s WHERE id = %s", updates)
    return sorted(broken, key=lambda b: b["name"])


def _checked(system_id: UUID, sql: str) -> MetricCheck:
    check = check_metric(system_id, sql)
    if check.error:
        raise ApiException(400, f"{STAGE_LABEL.get(check.stage, '확인 실패')}: {check.error}")
    return check


def _values(data: MetricInput, check: MetricCheck) -> tuple:
    return (
        data.name,
        data.description,
        json.dumps(data.synonyms, ensure_ascii=False),
        data.sql.strip().rstrip(";").strip(),
        json.dumps(check.ast, ensure_ascii=False),
    )


def _save_tables(cur, system_id: UUID, metric_id: UUID, names: list[str]) -> None:
    """지표가 쓰는 테이블. 질문 처리에서 테이블마다 지표 설명을 붙일 때 쓴다."""
    tables = load_tables(system_id)
    cur.execute("DELETE FROM metric_table WHERE metric_id = %s", (str(metric_id),))
    links = []
    for i, n in enumerate(names):
        if n in tables:
            links.append((str(metric_id), tables[n]["id"], "base" if i == 0 else "join"))
    cur.executemany("INSERT INTO metric_table (metric_id, table_id, role) VALUES (%s, %s, %s)", links)


def _record_history(cur, system_id: UUID, metric_id: UUID, version: int, action: str) -> None:
    cur.execute(
        """
        INSERT INTO metric_history (system_id, metric_id, version, action, snapshot)
        SELECT m.system_id, m.id, %s, %s,
               jsonb_build_object('name', m.name, 'description', m.description, 'synonyms', m.synonyms,
                                  'sql', m.source_sql, 'ast', m.ast)
          FROM metric m
         WHERE m.id = %s AND m.system_id = %s
    """,
        (version, action, str(metric_id), str(system_id)),
    )


def _guard_duplicate_name(system_id: UUID, name: str, except_id: UUID | None = None) -> None:
    existing = db.one(
        "SELECT id FROM metric WHERE system_id = %s AND lower(name) = lower(%s) AND id <> %s",
        str(system_id),
        name,
        str(except_id or uuid4()),
    )
    if existing:
        raise ApiException(409, f"'{name}' 이름의 지표가 이미 있습니다")


def _compiler(system_id: UUID) -> Compiler:
    row = db.one("SELECT driver FROM meta_system WHERE id = %s", str(system_id))
    tables = load_tables(system_id)
    return Compiler({"driver": row["driver"] if row else "postgresql", "tables": list(tables.values())})


def _row_to_metric(row: dict, compiler: Compiler) -> Metric:
    try:
        compiled = compiler.compile(row["ast"])
    except ValueError:
        compiled = None
    return Metric(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        synonyms=row["synonyms"] or [],
        sql=row["source_sql"],
        ast=row["ast"],
        compiled_sql=compiled,
        tables=row.get("tables") or [],
        status=row["status"],
        broken_reason=row["broken_reason"],
        version=row["version"],
        updated_at=row["updated_at"].isoformat() if row.get("updated_at") else None,
    )
