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


def col(name, generic, is_dttm=False):
    return {"column_name": name, "type_generic": generic, "is_dttm": is_dttm}


def test_chart_kind_follows_result_columns():
    viz, params = superset_link.chart_params(3, [col("day", 2, True), col("vehicle_id", 1), col("wk_area", 0)])
    assert viz == "echarts_timeseries_line"
    assert params["x_axis"] == "day" and params["groupby"] == ["vehicle_id"]
    assert params["metrics"][0]["column"] == {"column_name": "wk_area"}
    assert params["limit"] == superset_link.SERIES_LIMIT and params["datasource"] == "3__table"

    viz, params = superset_link.chart_params(3, [col("vehicle_id", 1), col("n", 0)])
    assert viz == "echarts_timeseries_bar" and params["x_axis"] == "vehicle_id"
    assert params["x_axis_sort"] == "n" and params["x_axis_sort_asc"] is False

    viz, params = superset_link.chart_params(3, [col("vehicles", 0)])
    assert viz == "big_number_total" and params["metric"]["label"] == "vehicles"

    # 숫자 컬럼이 많으면 원본 행 조회로 보고 표로 둔다
    many = [col("t", 2, True), col("vehicle_id", 1), col("a", 0), col("b", 0), col("c", 0), col("d", 0)]
    viz, params = superset_link.chart_params(3, many)
    assert viz == "table" and params["all_columns"] == ["t", "vehicle_id", "a", "b", "c", "d"]


def test_type_name_is_used_when_generic_type_is_missing():
    viz, _ = superset_link.chart_params(3, [{"column_name": "k", "type": "VARCHAR"}, {"column_name": "v", "type": "BIGINT"}])
    assert viz == "echarts_timeseries_bar"


def test_create_chart_reuses_dataset_and_chart_of_same_sql(monkeypatch):
    session, _ = setup(
        monkeypatch,
        {
            ("GET", "/api/v1/dataset/"): (200, {"result": [{"id": 11}]}),
            ("GET", "/api/v1/dataset/11"): (200, {"result": {"columns": [col("vehicles", 0)]}}),
            ("GET", "/api/v1/chart/"): (200, {"result": [{"id": 10}]}),
        },
    )
    monkeypatch.setattr(
        superset_link.db, "one", lambda sql, *args: {"name": "A", "db_schema": "public", "superset_database_id": 1}
    )
    out = superset_link.create_chart("sid", "SELECT count(*) AS vehicles FROM t;", "차량 수")
    assert out == {"url": "http://superset:8088/explore/?slice_id=10", "viz_type": "big_number_total", "dataset_id": 11, "chart_id": 10}
    for method, path, body, headers in session.calls:
        assert method == "GET" or path.startswith("/api/v1/security")


def test_create_chart_makes_dataset_and_chart(monkeypatch):
    session, _ = setup(
        monkeypatch,
        {
            ("GET", "/api/v1/dataset/"): (200, {"result": []}),
            ("POST", "/api/v1/dataset/"): (201, {"id": 12}),
            ("GET", "/api/v1/dataset/12"): (200, {"result": {"columns": [col("k", 1), col("v", 0)]}}),
            ("GET", "/api/v1/chart/"): (200, {"result": []}),
            ("POST", "/api/v1/chart/"): (201, {"id": 20}),
        },
    )
    monkeypatch.setattr(
        superset_link.db, "one", lambda sql, *args: {"name": "A", "db_schema": "public", "superset_database_id": 1}
    )
    out = superset_link.create_chart("sid", "SELECT k, v FROM t;", "it's 차트!")
    assert out["chart_id"] == 20 and out["viz_type"] == "echarts_timeseries_bar"
    dataset_body = session.calls[3][2]
    # 끝의 ; 는 떼고, 이름에 SQL 해시를 넣는다
    assert dataset_body["sql"] == "SELECT k, v FROM t" and dataset_body["schema"] == "public"
    assert dataset_body["table_name"].startswith("nl2sql it's 차트! [")
    # 목록 검색의 rison 문자열은 ' 와 ! 를 ! 로 이스케이프한다
    assert superset_link._rison("it's 차트!") == "'it!'s 차트!!'"
    chart_body = session.calls[-1][2]
    assert chart_body["datasource_id"] == 12 and chart_body["slice_name"] == "it's 차트!"


def test_superset_endpoints_use_sql_from_history_only(monkeypatch):
    """화면은 질문 기록 id 만 보낸다 — Superset 으로 가는 SQL 은 기록의 생성 SQL 이다"""
    import app as app_module
    from types import SimpleNamespace

    entry = SimpleNamespace(system_id="sid", sql="SELECT 1", question="질문")
    monkeypatch.setattr(app_module, "get_history", lambda entry_id: entry)
    seen = []
    monkeypatch.setattr(
        app_module,
        "create_chart",
        lambda *args: seen.append(args) or {"url": "u", "viz_type": "table", "dataset_id": 1, "chart_id": 2},
    )
    monkeypatch.setattr(app_module, "sqllab_link", lambda *args: seen.append(args) or "u")
    app_module.post_superset_chart("eid")
    app_module.post_superset_sqllab("eid")
    assert seen == [("sid", "SELECT 1", "질문"), ("sid", "SELECT 1", "질문")]

    entry.sql = None
    with pytest.raises(ApiException) as caught:
        app_module.post_superset_chart("eid")
    assert caught.value.status_code == 400
