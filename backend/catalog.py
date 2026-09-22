import psycopg
import pymysql
import oracledb
from psycopg.conninfo import make_conninfo
import logging

from models import Driver, ApiException

log = logging.getLogger("nl2sql.catalog")

CONNECT_TIMEOUT = 10


def read_catalog(row: dict) -> list[dict]:
    """대상 DB의 스키마 읽기"""
    driver = Driver(row["driver"])
    if driver is Driver.MYSQL:
        return read_mysql(row)
    elif driver is Driver.ORACLE:
        return read_oracle(row)
    else:
        return read_postgresql(row)


def read_postgresql(row: dict) -> list[dict]:
    """PostgreSQL 스키마 읽기"""
    schema = row["db_schema"] or "public"
    conninfo = make_conninfo(
        host=row["host"],
        port=row["port"],
        user=row["username"] or None,
        password=row["password"] or None,
        dbname=row["db_name"],
        connect_timeout=CONNECT_TIMEOUT,
    )
    try:
        connection = psycopg.connect(conninfo)
    except Exception as error:
        raise _unreachable(row, error) from error

    try:
        with connection.cursor() as cur:
            cur.execute("""
                SELECT c.relname, coalesce(obj_description(c.oid, 'pg_class'), '')
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = %s AND c.relkind IN ('r','v','m','p','f')
                 ORDER BY c.relname
            """, (schema,))
            tables = {name: {"name": name, "description": comment, "columns": []}
                      for name, comment in cur.fetchall()}

            cur.execute("""
                SELECT c.relname, a.attname,
                       format_type(a.atttypid, a.atttypmod),
                       coalesce(col_description(c.oid, a.attnum), '')
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  JOIN pg_attribute a ON a.attrelid = c.oid
                 WHERE n.nspname = %s AND c.relkind IN ('r','v','m','p','f')
                   AND a.attnum > 0 AND NOT a.attisdropped
                 ORDER BY c.relname, a.attnum
            """, (schema,))
            for table_name, column, data_type, comment in cur.fetchall():
                if table_name in tables:
                    tables[table_name]["columns"].append({
                        "name": column,
                        "type": _text(data_type),
                        "description": _text(comment)
                    })
    finally:
        connection.close()
    return list(tables.values())


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
                "SELECT TABLE_NAME, TABLE_COMMENT FROM information_schema.TABLES"
                " WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE','VIEW')"
                " ORDER BY TABLE_NAME",
                (row["db_name"],)
            )
            tables = {
                _text(n): {"name": _text(n), "description": _text(c), "columns": []}
                for n, c in cur.fetchall()
            }

            cur.execute(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT"
                "  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s"
                " ORDER BY TABLE_NAME, ORDINAL_POSITION",
                (row["db_name"],)
            )
            for table_name, column, column_type, comment in cur.fetchall():
                table = tables.get(_text(table_name))
                if table is not None:
                    table["columns"].append({
                        "name": _text(column),
                        "type": _text(column_type),
                        "description": _text(comment)
                    })
    finally:
        connection.close()
    return list(tables.values())


def read_oracle(row: dict) -> list[dict]:
    """Oracle 스키마 읽기"""
    owner = (row["db_schema"] or row["username"] or "").strip().upper()
    if not owner:
        raise ApiException(400, "Oracle 데이터소스는 스키마나 계정 중 하나가 필요합니다")

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
            cur.execute("""
                SELECT table_name, comments FROM all_tab_comments
                 WHERE owner = :owner AND table_type IN ('TABLE','VIEW')
                 ORDER BY table_name
            """, owner=owner)
            tables = {
                _text(n): {"name": _text(n), "description": _text(c), "columns": []}
                for n, c in cur.fetchall()
            }

            cur.execute("""
                SELECT c.table_name, c.column_name, c.data_type, c.data_length,
                       c.data_precision, c.data_scale, cc.comments
                  FROM all_tab_columns c
                  LEFT JOIN all_col_comments cc
                    ON cc.owner = c.owner AND cc.table_name = c.table_name
                   AND cc.column_name = c.column_name
                 WHERE c.owner = :owner
                 ORDER BY c.table_name, c.column_id
            """, owner=owner)
            for tname, column, type_name, length, precision, scale, comment in cur.fetchall():
                table = tables.get(_text(tname))
                if table is not None:
                    table["columns"].append({
                        "name": _text(column),
                        "type": _oracle_type(type_name, length, precision, scale),
                        "description": _text(comment)
                    })
    finally:
        connection.close()
    return list(tables.values())


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
        f"{reason[0] if reason else type(error).__name__}"
    )
