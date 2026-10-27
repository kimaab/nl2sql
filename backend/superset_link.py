"""Superset 연동: nl2sql 시스템을 Superset 의 DB 연결로 등록한다.

데이터는 옮기지 않는다 — Superset 은 차트를 그리거나 SQL Lab 에서 실행할 때마다 대상 DB 에 직접 쿼리한다.
접속 정보(비밀번호 포함)는 nl2sql 이 가진 것을 그대로 보내, 운영자가 Superset 에 두 번 입력하지 않게 한다.
Superset 의 연결 id 는 meta_system.superset_database_id 에 남긴다 — 'Superset 에서 열기'(sqllab_link) 가 쓴다.

설정 (backend/.env): SUPERSET_URL, SUPERSET_USERNAME, SUPERSET_PASSWORD
"""

import hashlib
import json
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
    row = _linked(system_id)
    state = {
        "dbId": row["superset_database_id"],
        "sql": sql,
        "name": (name or row["name"])[:100],
        "autorun": False,
    }
    if row["db_schemas"]:  # 기본 스키마. 다른 스키마의 테이블은 SQL 에 '스키마.테이블' 로 적혀 있다
        state["schema"] = row["db_schemas"][0]
    api = _Api()
    key = api.call("POST", "/api/v1/sqllab/permalink", json=state)["key"]
    # 응답의 url 은 Superset 이 요청받은 호스트로 만든다 — 브라우저가 여는 주소는 SUPERSET_URL 로 맞춘다
    return f"{api.base}/sqllab/p/{key}"


def create_chart(system_id: UUID, sql: str, name: str = "") -> dict:
    """SQL 로 Superset 차트를 만들고 그 편집 화면(Explore) 링크를 돌려준다. → {url, viz_type, dataset_id, chart_id}

    1. SQL 을 가상 데이터셋으로 등록한다. Superset 이 SQL 을 실행해 결과 컬럼과 타입을 읽는다.
    2. 컬럼 모양으로 차트 종류를 정해 차트를 저장한다 (chart_params).
    같은 SQL 이면 데이터셋과 차트를 새로 만들지 않고 전에 만든 것을 연다 — 질문을 다시 해도 쌓이지 않는다.
    """
    row = _linked(system_id)
    sql = sql.strip().rstrip(";").strip()  # 가상 데이터셋은 문장 하나만 받는다
    title = (name or row["name"]).strip()[:80]
    api = _Api()
    dataset_id = _dataset(api, row, sql, title)
    columns = api.call("GET", f"/api/v1/dataset/{dataset_id}")["result"]["columns"]
    viz_type, params = chart_params(dataset_id, columns)
    chart_id = _chart(api, dataset_id, title, viz_type, params)
    return {
        "url": f"{api.base}/explore/?slice_id={chart_id}",
        "viz_type": viz_type,
        "dataset_id": dataset_id,
        "chart_id": chart_id,
    }


def chart_params(dataset_id: int, columns: list[dict]) -> tuple[str, dict]:
    """결과 컬럼 모양으로 차트 종류와 설정을 정한다.

    시각 + 숫자 → 선 차트 (문자 컬럼이 있으면 그 값마다 선 하나)
    문자 + 숫자 → 막대 차트
    숫자 하나만 → 큰 숫자
    그 밖 → 표 (숫자 컬럼이 MAX_METRICS 보다 많을 때도)
    생성된 SQL 은 이미 집계한 결과라 차트는 다시 묶지 않는다 — 같은 칸에 한 행이면 SUM 이 그 값이다.
    """
    times, numbers, texts = [], [], []
    for c in columns:
        kind = _kind(c)
        if kind == "time":
            times.append(c["column_name"])
        elif kind == "number":
            numbers.append(c["column_name"])
        else:
            texts.append(c["column_name"])
    base = {"datasource": f"{dataset_id}__table", "adhoc_filters": [], "row_limit": ROW_LIMIT}
    # 숫자 컬럼이 많으면 대개 원본 행 조회다 — 한 차트에 다 그리면 읽을 수 없어 표로 둔다
    plottable = 0 < len(numbers) <= MAX_METRICS

    if times and plottable:
        params = {
            **base,
            "viz_type": "echarts_timeseries_line",
            "x_axis": times[0],
            "time_grain_sqla": None,  # 결과의 시각을 그대로 쓴다
            "metrics": _metrics(numbers),
            "groupby": texts[:1],
            # 선이 너무 많으면 첫 숫자 컬럼 합이 큰 SERIES_LIMIT 개만 그린다 (기준 지표가 없으면 제한이 먹지 않는다)
            "limit": SERIES_LIMIT if texts else None,  # 화면의 Series limit
            "timeseries_limit_metric": _metrics(numbers)[0],  # 화면의 Sort query by
            "order_desc": True,
            "show_legend": True,
            "rich_tooltip": True,
        }
        return "echarts_timeseries_line", params
    if texts and plottable:
        params = {
            **base,
            "viz_type": "echarts_timeseries_bar",
            "x_axis": texts[0],
            "metrics": _metrics(numbers),
            "groupby": texts[1:2],
            # 값이 큰 막대부터
            "x_axis_sort": numbers[0],
            "x_axis_sort_asc": False,
            "show_legend": True,
            "rich_tooltip": True,
        }
        return "echarts_timeseries_bar", params
    if len(numbers) == 1 and len(columns) == 1:
        params = {**base, "viz_type": "big_number_total", "metric": _metrics(numbers)[0]}
        return "big_number_total", params
    all_columns = []
    for c in columns:
        all_columns.append(c["column_name"])
    params = {**base, "viz_type": "table", "query_mode": "raw", "all_columns": all_columns}
    return "table", params


# 차트가 읽을 최대 행 수, 선 차트에서 그릴 최대 선 수 (값이 큰 순)
ROW_LIMIT = 10000
SERIES_LIMIT = 20
# 한 차트에 그릴 최대 숫자 컬럼 수
MAX_METRICS = 3
# Superset 의 GenericDataType: 0 숫자, 1 문자, 2 시각, 3 참거짓
_GENERIC = {0: "number", 1: "text", 2: "time", 3: "text"}


def _kind(column: dict) -> str:
    if column.get("is_dttm"):
        return "time"
    generic = column.get("type_generic")
    if generic in _GENERIC:
        return _GENERIC[generic]
    type_name = (column.get("type") or "").upper()
    for word in ("INT", "NUMERIC", "DECIMAL", "DOUBLE", "FLOAT", "REAL"):
        if word in type_name:
            return "number"
    return "text"


def _metrics(numbers: list[str]) -> list[dict]:
    out = []
    for name in numbers:
        out.append(
            {
                "expressionType": "SIMPLE",
                "column": {"column_name": name},
                "aggregate": "SUM",
                "label": name,
                "hasCustomLabel": True,
                "optionName": f"metric_{name}",
            }
        )
    return out


def _dataset(api: "_Api", row: dict, sql: str, title: str) -> int:
    """SQL 의 가상 데이터셋 id. 이름에 SQL 해시를 넣어 같은 SQL 이면 전에 만든 것을 쓴다."""
    table_name = f"nl2sql {title} [{hashlib.sha1(sql.encode()).hexdigest()[:10]}]"
    found = api.call(
        "GET",
        "/api/v1/dataset/",
        params={"q": f"(filters:!((col:table_name,opr:eq,value:{_rison(table_name)})),page_size:1)"},
    )
    for d in found.get("result") or []:
        return d["id"]
    body = {"database": row["superset_database_id"], "table_name": table_name, "sql": sql}
    if row["db_schemas"]:
        body["schema"] = row["db_schemas"][0]
    dataset_id = api.call("POST", "/api/v1/dataset/", json=body)["id"]
    log.info("superset dataset created: %s → #%s", table_name, dataset_id)
    return dataset_id


def _chart(api: "_Api", dataset_id: int, title: str, viz_type: str, params: dict) -> int:
    """데이터셋의 차트 id. 이 데이터셋으로 만든 차트가 있으면 그것을 연다 (사람이 고친 설정을 덮지 않는다)."""
    found = api.call(
        "GET",
        "/api/v1/chart/",
        params={"q": f"(filters:!((col:datasource_id,opr:eq,value:{dataset_id})),page_size:1)"},
    )
    for c in found.get("result") or []:
        return c["id"]
    body = {
        "slice_name": title,
        "viz_type": viz_type,
        "datasource_id": dataset_id,
        "datasource_type": "table",
        "params": json.dumps(params, ensure_ascii=False),
    }
    chart_id = api.call("POST", "/api/v1/chart/", json=body)["id"]
    log.info("superset chart created: %s (%s) → #%s", title, viz_type, chart_id)
    return chart_id


def _rison(text: str) -> str:
    """rison 문자열 (Superset 목록 API 의 q 인자)"""
    return "'" + text.replace("!", "!!").replace("'", "!'") + "'"


def _linked(system_id: UUID) -> dict:
    """Superset 에 연결한 시스템 행. 연결하지 않았으면 400."""
    row = db.one("SELECT name, db_schemas, superset_database_id FROM meta_system WHERE id = %s", str(system_id))
    if row is None:
        raise ApiException(404, "시스템을 찾을 수 없습니다")
    if not row["superset_database_id"]:
        raise ApiException(400, f"{row['name']} 은 아직 Superset 에 연결하지 않았습니다 (시스템 목록의 Superset 연결)")
    return row


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
        # 자체 서명 인증서로 띄운 Superset 은 그 CA 로 검증한다 (superset/certs/ca.crt)
        ca_bundle = os.environ.get("SUPERSET_CA_BUNDLE", "").strip()
        if ca_bundle:
            self.session.verify = ca_bundle
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
