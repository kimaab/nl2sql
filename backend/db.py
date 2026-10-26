import logging
import os
import time
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("nl2sql.sql")

_pool: ConnectionPool | None = None


class LoggingCursor(psycopg.Cursor):
    """SQL_LOG=1 이면 실행한 쿼리를 파라미터 값을 채운 모양으로 남긴다 (스프링 show-sql 처럼)"""

    def execute(self, query, params=None, **kwargs):
        if not sql_log_enabled():
            return super().execute(query, params, **kwargs)
        started = time.perf_counter()
        try:
            return super().execute(query, params, **kwargs)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            log.info("%.0fms %s", elapsed, _filled(self.connection, query, params))

    def executemany(self, query, params_seq, **kwargs):
        if not sql_log_enabled():
            return super().executemany(query, params_seq, **kwargs)
        params_seq = list(params_seq)
        started = time.perf_counter()
        try:
            return super().executemany(query, params_seq, **kwargs)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            log.info("%.0fms (%d건) %s", elapsed, len(params_seq), _filled(self.connection, query, None))


def sql_log_enabled() -> bool:
    return os.environ.get("SQL_LOG", "").lower() in ("1", "true", "yes")


def _filled(conn, query, params) -> str:
    """파라미터 값을 채운 SQL 문자열 (못 채우면 쿼리와 값을 따로)"""
    try:
        text = psycopg.ClientCursor(conn).mogrify(query, params)
    except Exception:
        text = f"{query} -- params={params!r}"
    return " ".join(str(text).split())


def init_pool():
    """DB 커넥션 풀 초기화"""
    global _pool
    db_url = os.environ.get("DB_URL")
    if not db_url:
        raise RuntimeError("DB_URL이 설정되지 않았습니다 (backend/.env를 확인하십시오)")
    _pool = ConnectionPool(db_url, open=True, timeout=10, kwargs={"cursor_factory": LoggingCursor})
    _pool.wait(timeout=10)


def get_pool() -> ConnectionPool:
    """커넥션 풀 반환"""
    if _pool is None:
        init_pool()
    return _pool


def close_pool():
    """커넥션 풀 종료"""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection():
    """커넥션 하나를 빌려준다. 블록을 정상적으로 빠져나가면 커밋된다."""
    with get_pool().connection() as conn:
        yield conn


def one(query: str, *args) -> dict | None:
    """한 행 조회"""
    with connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, args)
            return cur.fetchone()


def query(query: str, *args) -> list[dict]:
    """여러 행 조회"""
    with connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, args)
            return cur.fetchall()
