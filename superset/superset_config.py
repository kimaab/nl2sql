"""Superset 설정. 컨테이너의 /app/pythonpath/superset_config.py 로 붙는다.

Redis 와 Celery 없이 Superset 하나만 돈다. 그래서 쓰지 않는 기능:
비동기 SQL Lab 실행, 알림과 리포트, 대시보드 썸네일 (모두 기본값이 꺼져 있다).
"""

import os

SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]

# 메타데이터 DB: nl2sql 메타데이터 DB 와 같은 PostgreSQL 서버의 superset 데이터베이스
SQLALCHEMY_DATABASE_URI = (
    f"postgresql+psycopg2://{os.environ['SUPERSET_DB_USER']}:{os.environ['SUPERSET_DB_PASSWORD']}"
    f"@{os.environ['SUPERSET_DB_HOST']}:{os.environ.get('SUPERSET_DB_PORT', '5432')}/{os.environ['SUPERSET_DB_NAME']}"
)

# 캐시는 프로세스 메모리 (gunicorn 워커 1개 기준)
CACHE_CONFIG = {"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300, "CACHE_KEY_PREFIX": "superset_"}
DATA_CACHE_CONFIG = {**CACHE_CONFIG, "CACHE_KEY_PREFIX": "superset_data_"}
FILTER_STATE_CACHE_CONFIG = {**CACHE_CONFIG, "CACHE_KEY_PREFIX": "superset_filter_"}
EXPLORE_FORM_DATA_CACHE_CONFIG = {**CACHE_CONFIG, "CACHE_KEY_PREFIX": "superset_explore_"}

# 로그인 시도 제한 저장소 (Redis 대신 메모리)
RATELIMIT_STORAGE_URI = "memory://"
