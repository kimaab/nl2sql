#!/bin/sh
# Superset 첫 준비. superset-init 컨테이너가 한 번 돌고 끝난다 — 다시 돌아도 안전하다.
#   1. 메타데이터용 superset 데이터베이스가 없으면 만든다
#   2. 스키마 마이그레이션
#   3. 관리자 계정 (이미 있으면 그대로 둔다)
#   4. 기본 역할과 권한
#   5. HTTPS 인증서 (없거나 이름이 바뀌었으면 만든다)
set -e

python - <<'EOF'
import os

import psycopg2

name = os.environ["SUPERSET_DB_NAME"]
conn = psycopg2.connect(
    host=os.environ["SUPERSET_DB_HOST"],
    port=os.environ.get("SUPERSET_DB_PORT", "5432"),
    user=os.environ["SUPERSET_DB_USER"],
    password=os.environ["SUPERSET_DB_PASSWORD"],
    dbname="postgres",
    connect_timeout=10,
)
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
    if cur.fetchone() is None:
        cur.execute(f'CREATE DATABASE "{name}"')
        print(f"데이터베이스를 만들었습니다: {name}")
    else:
        print(f"데이터베이스가 이미 있습니다: {name}")
conn.close()
EOF

superset db upgrade
superset fab create-admin \
    --username "${ADMIN_USERNAME:-admin}" \
    --firstname Superset \
    --lastname Admin \
    --email "${ADMIN_EMAIL:-admin@localhost}" \
    --password "${ADMIN_PASSWORD:?ADMIN_PASSWORD 를 .env 에 적으십시오}"
superset init
python /app/make_cert.py
