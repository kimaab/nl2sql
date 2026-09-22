import os
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def init_pool():
    """DB 커넥션 풀 초기화"""
    global _pool
    db_url = os.environ.get("DB_URL")
    if not db_url:
        raise RuntimeError("DB_URL이 설정되지 않았습니다 (backend/.env를 확인하십시오)")
    _pool = ConnectionPool(db_url, open=True, timeout=10)
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
