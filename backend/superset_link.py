"""Superset 연동: nl2sql 시스템을 Superset 의 DB 연결로 등록한다.

데이터는 옮기지 않는다 — Superset 은 차트를 그리거나 SQL Lab 에서 실행할 때마다 대상 DB 에 직접 쿼리한다.
접속 정보(비밀번호 포함)는 nl2sql 이 가진 것을 그대로 보내, 운영자가 Superset 에 두 번 입력하지 않게 한다.
Superset 의 연결 id 는 meta_system.superset_database_id 에 남긴다 — 'Superset 에서 열기'(sqllab_link) 가 쓴다.

설정 (backend/.env): SUPERSET_URL, SUPERSET_USERNAME, SUPERSET_PASSWORD
"""

import logging
import os
from urllib.parse import quote
from uuid import UUID

import requests

import db
from models import ApiException, System
from systems import connection_row, get_system

log = logging.getLogger("nl2sql.superset")

TIMEOUT_S = 30
# Superset 이미지에 넣은 드라이버 (superset/Dockerfile). Oracle 은 없다.
SCHEMES = {"postgresql": "postgresql+psycopg2", "mysql": "mysql+pymysql"}


def link_database(system_id: UUID) -> System:
    """시스템을 Superset DB 연결로 등록한다. 이미 연결했으면 접속 정보를 지금 것으로 갱신한다.

    Superset 은 연결을 만들 때 대상 DB 에 접속해 본다 — 접속하지 못하면 등록되지 않고 그 이유를 돌려준다.
    """
    row = connection_row(system_id)
    scheme = SCHEMES.get(row["driver"])
    if scheme is None:
        raise ApiException(400, f"Superset 에 {row['driver']} 드라이버가 없어 연결할 수 없습니다 (superset/Dockerfile)")
    body = {
        "database_name": row["name"],
        "sqlalchemy_uri": _uri(scheme, row),
        "expose_in_sqllab": True,
        "allow_dml": False,  # 조회만 한다
        "allow_run_async": False,  # Celery 없이 띄웠다 (superset/docker-compose.yml)
    }
    api = _Api()
    database_id = _find(api, row)
    if database_id is None:
        database_id = api.call("POST", "/api/v1/database/", json=body)["id"]
        log.info("superset database created: %s → #%s", row["name"], database_id)
    else:
        api.call("PUT", f"/api/v1/database/{database_id}", json=body)
        log.info("superset database updated: %s → #%s", row["name"], database_id)
    _save_id(system_id, database_id)
    return get_system(system_id)


def sqllab_link(system_id: UUID, sql: str, name: str = "") -> str:
    """SQL 을 Superset SQL Lab 에 열어 두는 링크. 실행은 사람이 SQL Lab 에서 누른다.

    SQL 을 URL 에 싣지 않고 Superset 의 permalink 로 저장한다 — 긴 SQL 도 잘리지 않는다.
    """
    row = db.one("SELECT name, db_schema, superset_database_id FROM meta_system WHERE id = %s", str(system_id))
    if row is None:
        raise ApiException(404, "시스템을 찾을 수 없습니다")
    if not row["superset_database_id"]:
        raise ApiException(400, f"{row['name']} 은 아직 Superset 에 연결하지 않았습니다 (시스템 목록의 Superset 연결)")
    state = {
        "dbId": row["superset_database_id"],
        "sql": sql,
        "name": (name or row["name"])[:100],
        "autorun": False,
    }
    if row["db_schema"]:
        state["schema"] = row["db_schema"]
    api = _Api()
    key = api.call("POST", "/api/v1/sqllab/permalink", json=state)["key"]
    # 응답의 url 은 Superset 이 요청받은 호스트로 만든다 — 브라우저가 여는 주소는 SUPERSET_URL 로 맞춘다
    return f"{api.base}/sqllab/p/{key}"


def _uri(scheme: str, row: dict) -> str:
    user = quote(row["username"] or "", safe="")
    password = quote(row["password"] or "", safe="")
    auth = f"{user}:{password}@" if user else ""
    return f"{scheme}://{auth}{row['host']}:{row['port']}/{row['db_name']}"


def _find(api: "_Api", row: dict) -> int | None:
    """이 시스템의 Superset 연결 id. 남겨 둔 id 가 Superset 에 없으면 이름으로 찾는다 (손으로 만든 연결도 이어 쓴다)."""
    saved = row.get("superset_database_id")
    if saved and api.call("GET", f"/api/v1/database/{saved}", missing_ok=True) is not None:
        return saved
    found = api.call("GET", "/api/v1/database/", params={"q": "(page_size:100)"})
    for d in found.get("result") or []:
        if d.get("database_name") == row["name"]:
            return d["id"]
    return None


def _save_id(system_id: UUID, database_id: int) -> None:
    with db.connection() as conn:
        conn.execute(
            "UPDATE meta_system SET superset_database_id = %s, updated_at = now() WHERE id = %s",
            (database_id, str(system_id)),
        )


class _Api:
    """Superset REST API. 로그인(JWT)과 CSRF 토큰을 한 세션에 들고 부른다 — 쓰기 요청은 CSRF 토큰이 없으면 거절된다."""

    def __init__(self):
        self.base = os.environ.get("SUPERSET_URL", "").strip().rstrip("/")
        user = os.environ.get("SUPERSET_USERNAME", "").strip()
        password = os.environ.get("SUPERSET_PASSWORD", "")
        if not self.base or not user or not password:
            raise ApiException(
                400, "Superset 연동 설정이 없습니다 (backend/.env 의 SUPERSET_URL, SUPERSET_USERNAME, SUPERSET_PASSWORD)"
            )
        self.session = requests.Session()
        login = self.call(
            "POST",
            "/api/v1/security/login",
            json={"username": user, "password": password, "provider": "db", "refresh": False},
        )
        self.session.headers["Authorization"] = f"Bearer {login['access_token']}"
        csrf = self.call("GET", "/api/v1/security/csrf_token/")["result"]
        self.session.headers.update({"X-CSRFToken": csrf, "Referer": self.base})

    def call(self, method: str, path: str, missing_ok: bool = False, **kwargs) -> dict | None:
        """→ 응답 JSON. missing_ok 이면 404 일 때 None."""
        try:
            res = self.session.request(method, self.base + path, timeout=TIMEOUT_S, **kwargs)
        except requests.RequestException as error:
            raise ApiException(502, f"Superset({self.base}) 에 접속하지 못했습니다: {_first_line(error)}") from error
        if missing_ok and res.status_code == 404:
            return None
        if res.status_code >= 400:
            raise ApiException(502, f"Superset 이 요청을 거절했습니다 ({method} {path}, {res.status_code}): {_reason(res)}")
        return res.json() if res.content else {}


def _reason(res) -> str:
    """Superset 오류 응답에서 사람이 읽을 이유 한 줄"""
    try:
        body = res.json()
    except ValueError:
        return res.text[:200]
    errors = body.get("errors") or []
    if errors:
        return str(errors[0].get("message") or errors[0])[:300]
    return str(body.get("message") or body)[:300]


def _first_line(error) -> str:
    lines = str(error).strip().splitlines()
    return lines[0] if lines else type(error).__name__
