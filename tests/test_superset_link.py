import pytest

import superset_link
from models import ApiException
from superset_link import link_database

ROW = {
    "name": "baroness개발서버",
    "driver": "postgresql",
    "host": "192.168.102.60",
    "port": 5432,
    "db_name": "baroness",
    "username": "baroness",
    "password": "p@ss:word/1",
    "superset_database_id": None,
}


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body
        self.content = b"x" if body is not None else b""
        self.text = str(body)

    def json(self):
        return self.body


class FakeSession:
    """경로마다 정해 둔 답을 주고, 받은 요청과 그때의 헤더를 남긴다"""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def request(self, method, url, timeout=None, **kwargs):
        path = url.replace("http://superset:8088", "")
        self.calls.append((method, path, kwargs.get("json"), dict(self.headers)))
        status, body = self.routes[(method, path)]
        return FakeResponse(status, body)


def setup(monkeypatch, routes, row=ROW):
    session = FakeSession(
        {
            ("POST", "/api/v1/security/login"): (200, {"access_token": "jwt"}),
            ("GET", "/api/v1/security/csrf_token/"): (200, {"result": "csrf"}),
            **routes,
        }
    )
    monkeypatch.setenv("SUPERSET_URL", "http://superset:8088/")
    monkeypatch.setenv("SUPERSET_USERNAME", "admin")
    monkeypatch.setenv("SUPERSET_PASSWORD", "pw")
    monkeypatch.setattr(superset_link.requests, "Session", lambda: session)
    monkeypatch.setattr(superset_link, "connection_row", lambda sid: dict(row))
    monkeypatch.setattr(superset_link, "get_system", lambda sid: "system")
    saved = []
    monkeypatch.setattr(superset_link, "_save_id", lambda sid, did: saved.append(did))
    return session, saved


def test_creates_connection_with_csrf_and_encoded_password(monkeypatch):
    session, saved = setup(
        monkeypatch,
        {
            ("GET", "/api/v1/database/"): (200, {"result": [{"id": 3, "database_name": "다른 DB"}]}),
            ("POST", "/api/v1/database/"): (201, {"id": 7}),
        },
    )
    assert link_database("sid") == "system"
    method, path, body, headers = session.calls[-1]
    assert (method, path) == ("POST", "/api/v1/database/")
    # 쓰기 요청은 JWT 와 CSRF 토큰을 함께 보낸다
    assert headers["Authorization"] == "Bearer jwt" and headers["X-CSRFToken"] == "csrf"
    assert body["sqlalchemy_uri"] == "postgresql+psycopg2://baroness:p%40ss%3Aword%2F1@192.168.102.60:5432/baroness"
    assert body["database_name"] == "baroness개발서버"
    assert body["allow_dml"] is False and body["allow_run_async"] is False
    assert saved == [7]


def test_updates_saved_connection(monkeypatch):
    session, saved = setup(
        monkeypatch,
        {
            ("GET", "/api/v1/database/5"): (200, {"id": 5}),
            ("PUT", "/api/v1/database/5"): (200, {"id": 5}),
        },
        row={**ROW, "superset_database_id": 5},
    )
    link_database("sid")
    assert session.calls[-1][:2] == ("PUT", "/api/v1/database/5")
    assert saved == [5]


def test_saved_id_missing_in_superset_falls_back_to_name(monkeypatch):
    session, saved = setup(
        monkeypatch,
        {
            ("GET", "/api/v1/database/5"): (404, {"message": "Not found"}),
            ("GET", "/api/v1/database/"): (200, {"result": [{"id": 9, "database_name": "baroness개발서버"}]}),
            ("PUT", "/api/v1/database/9"): (200, {"id": 9}),
        },
        row={**ROW, "superset_database_id": 5},
    )
    link_database("sid")
    assert session.calls[-1][:2] == ("PUT", "/api/v1/database/9")
    assert saved == [9]


def test_superset_rejection_is_reported(monkeypatch):
    setup(
        monkeypatch,
        {
            ("GET", "/api/v1/database/"): (200, {"result": []}),
            ("POST", "/api/v1/database/"): (
                422,
                {"errors": [{"message": "Connection failed, please check your connection settings"}]},
            ),
        },
    )
    with pytest.raises(ApiException) as caught:
        link_database("sid")
    assert "Connection failed" in caught.value.detail


def test_oracle_and_missing_config_are_rejected(monkeypatch):
    setup(monkeypatch, {}, row={**ROW, "driver": "oracle"})
    with pytest.raises(ApiException) as caught:
        link_database("sid")
    assert "oracle 드라이버가 없어" in caught.value.detail

    setup(monkeypatch, {})
    monkeypatch.delenv("SUPERSET_PASSWORD")
    with pytest.raises(ApiException) as caught:
        link_database("sid")
    assert "SUPERSET_PASSWORD" in caught.value.detail


def test_sqllab_link_saves_permalink_under_superset_url(monkeypatch):
    session, _ = setup(
        monkeypatch,
        {("POST", "/api/v1/sqllab/permalink"): (201, {"key": "abc", "url": "http://superset:8088/sqllab/p/abc"})},
    )
    monkeypatch.setattr(
        superset_link.db,
        "one",
        lambda sql, *args: {"name": "baroness개발서버", "db_schema": "public", "superset_database_id": 1},
    )
    url = superset_link.sqllab_link("sid", "SELECT 1", "어제 수집된 차량 수")
    assert url == "http://superset:8088/sqllab/p/abc"
    method, path, body, headers = session.calls[-1]
    assert headers["X-CSRFToken"] == "csrf"
    assert body == {"dbId": 1, "sql": "SELECT 1", "name": "어제 수집된 차량 수", "autorun": False, "schema": "public"}


def test_sqllab_link_needs_superset_connection(monkeypatch):
    setup(monkeypatch, {})
    monkeypatch.setattr(
        superset_link.db, "one", lambda sql, *args: {"name": "A", "db_schema": "", "superset_database_id": None}
    )
    with pytest.raises(ApiException) as caught:
        superset_link.sqllab_link("sid", "SELECT 1")
    assert "Superset 에 연결하지 않았습니다" in caught.value.detail
