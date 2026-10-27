import psycopg
import pymysql
import oracledb
from psycopg.conninfo import make_conninfo
import logging

from db import LoggingCursor
from models import Driver, ApiException

log = logging.getLogger("nl2sql.catalog")

CONNECT_TIMEOUT = 10


def read_catalog(row: dict) -> tuple[list[dict], list[dict] | None]:
    """대상 DB의 스키마와 FK 관계 읽기.

    관계는 None 일 수 있다 — FK 카탈로그를 읽을 권한이 없을 때다. 그때 기존
    FK 관계를 지우면 안 되므로 '빈 목록'과 구분한다.
    """
    driver = Driver(row["driver"])
    if driver is Driver.MYSQL:
        return read_mysql(row)
    elif driver is Driver.ORACLE:
        return read_oracle(row)
    else:
        return read_postgresql(row)


def schemas(row: dict) -> list[str]:
    """읽을 스키마. 첫 스키마가 기본 스키마다 — 그 테이블은 이름만, 나머지 스키마의 테이블은 '스키마.테이블' 로 쓴다.

    비우면 PostgreSQL 은 public, Oracle 은 접속 계정이다. MySQL 은 접속한 데이터베이스 하나만 읽는다.
    """
    driver = Driver(row["driver"])
    if driver is Driver.MYSQL:
        return [row["db_name"]]
    listed = []
    for s in row.get("db_schemas") or []:
        listed.append(s.strip().upper() if driver is Driver.ORACLE else s.strip())
    if listed:
        return listed
    if driver is Driver.ORACLE:
        owner = (row["username"] or "").strip().upper()
        if not owner:
            raise ApiException(400, "Oracle 시스템은 스키마나 계정 중 하나가 필요합니다")
        return [owner]
    return ["public"]


def table_parts(row: dict, table: dict) -> tuple[str, str]:
    """메타데이터 테이블 → (스키마, 대상 DB 의 테이블 이름)"""
    schema = table.get("schema") or ""
    if schema and table["name"].startswith(schema + "."):
        return schema, table["name"][len(schema) + 1:]
    return schemas(row)[0], table["name"]


def _qualified(schema: str, name: str, default: str) -> str:
    """기본 스키마의 테이블은 이름만, 나머지는 '스키마.테이블'"""
    return name if schema == default else f"{schema}.{name}"


def read_postgresql(row: dict) -> list[dict]:
    """PostgreSQL 스키마 읽기"""
    names = schemas(row)
    default = names[0]
    conninfo = make_conninfo(
        host=row["host"],
        port=row["port"],
        user=row["username"] or None,
        password=row["password"] or None,
        dbname=row["db_name"],
        connect_timeout=CONNECT_TIMEOUT,
    )
    try:
        connection = psycopg.connect(conninfo, cursor_factory=LoggingCursor)
    except Exception as error:
        raise _unreachable(row, error) from error

    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT n.nspname, c.relname, coalesce(obj_description(c.oid, 'pg_class'), '')
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','v','m','p','f')
                 ORDER BY n.nspname, c.relname
            """,
                (names,),
            )
            tables = _table_map(cur.fetchall(), default)

            cur.execute(
                """
                SELECT n.nspname, c.relname, a.attname,
                       format_type(a.atttypid, a.atttypmod),
                       coalesce(col_description(c.oid, a.attnum), '')
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  JOIN pg_attribute a ON a.attrelid = c.oid
                 WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','v','m','p','f')
                   AND a.attnum > 0 AND NOT a.attisdropped
                 ORDER BY n.nspname, c.relname, a.attnum
            """,
                (names,),
            )
            for schema, table_name, column, data_type, comment in cur.fetchall():
                table = tables.get(_qualified(schema, table_name, default))
                if table is not None:
                    table["columns"].append(
                        {
                            "name": column,
                            "type": _text(data_type),
                            "description": _text(comment),
                        }
                    )
            cur.execute(
                """
                SELECT n.nspname, c.relname, a.attname
                  FROM pg_index i
                  JOIN pg_class c ON c.oid = i.indrelid
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
                 WHERE i.indisprimary AND n.nspname = ANY(%s)
            """,
                (names,),
            )
            _mark_keys(tables, cur.fetchall(), default)
        relations = _read_relations(
            row,
            connection,
            """
            SELECT con.conname, n.nspname, cl.relname, att.attname, rn.nspname, rcl.relname, ratt.attname
              FROM pg_constraint con
              JOIN pg_class cl ON cl.oid = con.conrelid
              JOIN pg_namespace n ON n.oid = cl.relnamespace
              JOIN pg_class rcl ON rcl.oid = con.confrelid
              JOIN pg_namespace rn ON rn.oid = rcl.relnamespace
              JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(attnum, refnum, ord) ON true
              JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum
              JOIN pg_attribute ratt ON ratt.attrelid = con.confrelid AND ratt.attnum = k.refnum
             WHERE con.contype = 'f' AND n.nspname = ANY(%s)
             ORDER BY n.nspname, con.conname, k.ord
        """,
            (names,),
            tables,
            default,
        )
    finally:
        connection.close()
    return list(tables.values()), relations


def read_mysql(row: dict) -> list[dict]:
    """MySQL 스키마 읽기"""
    try:
        connection = pymysql.connect(
            host=row["host"],
            port=row["port"],
            user=row["username"],
            password=row["password"],
            database=row["db_name"],
            connect_timeout=CONNECT_TIMEOUT,
            read_timeout=30,
            charset="utf8mb4",
        )
    except Exception as error:
        raise _unreachable(row, error) from error

    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_COMMENT FROM information_schema.TABLES"
                " WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE','VIEW')"
                " ORDER BY TABLE_NAME",
                (row["db_name"],),
            )
            tables = _table_map(cur.fetchall(), row["db_name"])

            cur.execute(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT, COLUMN_KEY"
                "  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s"
                " ORDER BY TABLE_NAME, ORDINAL_POSITION",
                (row["db_name"],),
            )
            for table_name, column, column_type, comment, key in cur.fetchall():
                table = tables.get(_text(table_name))
                if table is not None:
                    table["columns"].append(
                        {
                            "name": _text(column),
                            "type": _text(column_type),
                            "description": _text(comment),
                            "is_pk": _text(key) == "PRI",
                        }
                    )
        relations = _read_relations(
            row,
            connection,
            """
            SELECT CONSTRAINT_NAME, TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME,
                   REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
              FROM information_schema.KEY_COLUMN_USAGE
             WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL
             ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION
        """,
            (row["db_name"], row["db_name"]),
            tables,
            row["db_name"],
        )
    finally:
        connection.close()
    return list(tables.values()), relations


def read_oracle(row: dict) -> list[dict]:
    """Oracle 스키마 읽기"""
    names = schemas(row)
    default = names[0]
    binds, marks = {}, []
    for i, name in enumerate(names):
        binds[f"s{i}"] = name
        marks.append(f":s{i}")
    owners = f"({', '.join(marks)})"

    try:
        connection = oracledb.connect(
            user=row["username"],
            password=row["password"],
            host=row["host"],
            port=row["port"],
            service_name=row["db_name"],
            tcp_connect_timeout=CONNECT_TIMEOUT,
        )
    except Exception as error:
        raise _unreachable(row, error) from error

    try:
        with connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT owner, table_name, comments FROM all_tab_comments
                 WHERE owner IN {owners} AND table_type IN ('TABLE','VIEW')
                 ORDER BY owner, table_name
            """,
                binds,
            )
            tables = _table_map(cur.fetchall(), default)

            cur.execute(
                f"""
                SELECT c.owner, c.table_name, c.column_name, c.data_type, c.data_length,
                       c.data_precision, c.data_scale, cc.comments
                  FROM all_tab_columns c
                  LEFT JOIN all_col_comments cc
                    ON cc.owner = c.owner AND cc.table_name = c.table_name
                   AND cc.column_name = c.column_name
                 WHERE c.owner IN {owners}
                 ORDER BY c.owner, c.table_name, c.column_id
            """,
                binds,
            )
            for (
                towner,
                tname,
                column,
                type_name,
                length,
                precision,
                scale,
                comment,
            ) in cur.fetchall():
                table = tables.get(_qualified(_text(towner), _text(tname), default))
                if table is not None:
                    table["columns"].append(
                        {
                            "name": _text(column),
                            "type": _oracle_type(type_name, length, precision, scale),
                            "description": _text(comment),
                        }
                    )
            cur.execute(
                f"""
                SELECT cc.owner, cc.table_name, cc.column_name
                  FROM all_constraints c
                  JOIN all_cons_columns cc ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
                 WHERE c.owner IN {owners} AND c.constraint_type = 'P'
            """,
                binds,
            )
            _mark_keys(tables, cur.fetchall(), default)
        relations = _read_relations(
            row,
            connection,
            f"""
            SELECT c.constraint_name, cc.owner, cc.table_name, cc.column_name, rc.owner, rc.table_name, rcc.column_name
              FROM all_constraints c
              JOIN all_cons_columns cc ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
              JOIN all_constraints rc ON rc.owner = c.r_owner AND rc.constraint_name = c.r_constraint_name
              JOIN all_cons_columns rcc ON rcc.owner = rc.owner AND rcc.constraint_name = rc.constraint_name
                                       AND rcc.position = cc.position
             WHERE c.owner IN {owners} AND c.constraint_type = 'R'
             ORDER BY c.owner, c.constraint_name, cc.position
        """,
            binds,
            tables,
            default,
        )
    finally:
        connection.close()
    return list(tables.values()), relations


def read_indexes(cur, row: dict, table: dict, columns: set) -> list[dict]:
    """메타데이터 테이블의 B-tree 인덱스 [{name, columns}] (columns 는 인덱스 순서).

    식·함수 인덱스 칸, 앞부분만 거는 칸(MySQL prefix), 모르는 컬럼이 나오면 그 앞까지만 남긴다 —
    인덱스는 앞 칸부터 쓰이므로 그 뒤 칸은 조회에 쓸 수 없다. 부분 인덱스(PostgreSQL WHERE)는 뺀다.
    """
    driver = Driver(row["driver"])
    schema, name = table_parts(row, table)
    if driver is Driver.MYSQL:
        cur.execute(
            "SELECT INDEX_NAME, COLUMN_NAME, SUB_PART FROM information_schema.STATISTICS"
            " WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_TYPE = 'BTREE'"
            " ORDER BY INDEX_NAME = 'PRIMARY' DESC, INDEX_NAME, SEQ_IN_INDEX",
            (schema, name),
        )
        fetched = []
        for name, column, sub_part in cur.fetchall():
            fetched.append((name, None if sub_part is not None else column))
    elif driver is Driver.ORACLE:
        cur.execute(
            """
            SELECT ic.index_name, ic.column_name
              FROM all_indexes i
              JOIN all_ind_columns ic ON ic.index_owner = i.owner AND ic.index_name = i.index_name
             WHERE i.table_owner = :1 AND i.table_name = :2 AND i.index_type = 'NORMAL'
               AND i.status IN ('VALID', 'N/A')
             ORDER BY ic.index_name, ic.column_position
        """,
            [schema, name],
        )
        fetched = cur.fetchall()
    else:
        cur.execute(
            """
            SELECT ic.relname, a.attname
              FROM pg_index x
              JOIN pg_class c ON c.oid = x.indrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_class ic ON ic.oid = x.indexrelid
              JOIN pg_am am ON am.oid = ic.relam
              JOIN LATERAL unnest(x.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) ON k.ord <= x.indnkeyatts
              LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum AND k.attnum > 0
             WHERE n.nspname = %s AND c.relname = %s
               AND x.indisvalid AND x.indpred IS NULL AND am.amname = 'btree'
             ORDER BY x.indisprimary DESC, ic.relname, k.ord
        """,
            (schema, name),
        )
        fetched = cur.fetchall()

    indexes, cut = [], set()
    for index_name, column in fetched:
        index_name, column = _text(index_name), _text(column) if column is not None else None
        if index_name in cut:
            continue
        if not indexes or indexes[-1]["name"] != index_name:
            indexes.append({"name": index_name, "columns": []})
        if column is None or column not in columns:
            cut.add(index_name)
            continue
        indexes[-1]["columns"].append(column)
    usable = []
    for index in indexes:
        if index["columns"]:
            usable.append(index)
    return usable


def _read_relations(
    row: dict, connection, sql: str, params, tables: dict, default: str
) -> list[dict] | None:
    """FK 를 관계 목록으로. 두 테이블이 모두 읽은 범위에 있을 때만 남긴다.

    실패해도 동기화는 계속한다 — 관계는 있으면 좋은 것이고, 테이블·컬럼이 본체다.
    """
    try:
        with connection.cursor() as cur:
            cur.execute(sql, params)
            fetched = cur.fetchall()
    except Exception as error:
        log.warning(
            "%s 의 FK 를 읽지 못했습니다 (기존 FK 관계를 유지합니다): %s",
            row["name"],
            error,
        )
        rollback = getattr(connection, "rollback", None)
        if rollback:
            rollback()
        return None
    out = []
    for name, schema, table, column, ref_schema, ref_table, ref_column in fetched:
        table = _qualified(_text(schema), _text(table), default)
        ref_table = _qualified(_text(ref_schema), _text(ref_table), default)
        if table in tables and ref_table in tables:
            out.append(
                {
                    "constraint": _text(name),
                    "left_table": table,
                    "left_column": _text(column),
                    "right_table": ref_table,
                    "right_column": _text(ref_column),
                }
            )
    return out


def _mark_keys(tables: dict, rows, default: str) -> None:
    """기본키 컬럼 표시. SQL 생성 단계에서 컬럼을 골라 실을 때 항상 포함한다."""
    keys = set()
    for schema, t, c in rows:
        keys.add((_qualified(_text(schema), _text(t), default), _text(c)))
    for table in tables.values():
        for column in table["columns"]:
            column["is_pk"] = (table["name"], column["name"]) in keys


def _table_map(rows, default: str) -> dict:
    """(스키마, 테이블 이름, 코멘트) 행 → {이름: 빈 컬럼 목록을 가진 테이블}

    schema 는 기본 스키마가 아닌 테이블만 채운다 (meta_table.db_schema).
    """
    tables = {}
    for schema, name, comment in rows:
        schema = _text(schema)
        name = _qualified(schema, _text(name), default)
        tables[name] = {
            "name": name,
            "schema": "" if schema == default else schema,
            "description": _text(comment),
            "columns": [],
        }
    return tables


def _text(value) -> str:
    """값을 문자열로 정규화"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value is not None else ""


def _oracle_type(name, length, precision, scale) -> str:
    """Oracle 타입을 길이까지 포함한 형태로"""
    base = _text(name).upper()
    if base == "NUMBER":
        if precision is None:
            return "NUMBER"
        return f"NUMBER({precision},{scale})" if scale else f"NUMBER({precision})"
    if base in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW") and length:
        return f"{base}({length})"
    return base


def _unreachable(row, error) -> ApiException:
    """DB 접속 실패 예외"""
    reason = str(error).strip().splitlines()
    return ApiException(
        502,
        f"{row['name']}의 스키마를 읽지 못했습니다 "
        f"({row['host']}:{row['port']}/{row['db_name']}): "
        f"{reason[0] if reason else type(error).__name__}",
    )
