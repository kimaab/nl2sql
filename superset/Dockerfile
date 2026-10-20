# Superset 6.1.0 (최종 정식 릴리스) + DB 드라이버
# 공식 이미지(lean)에는 DB 드라이버가 없다 — Superset 메타데이터 DB(PostgreSQL)에도 psycopg2 가 필요하다.
#   psycopg2-binary : 메타데이터 DB, PostgreSQL 대상 DB   postgresql+psycopg2://
#   pymysql         : MySQL 대상 DB                      mysql+pymysql://
# Oracle 은 SQLAlchemy 1.4 라 cx-Oracle + Instant Client 가 필요해 넣지 않았다.
FROM apache/superset:6.1.0

USER root
RUN uv pip install --no-cache --python /app/.venv/bin/python \
        psycopg2-binary==2.9.9 \
        pymysql==1.1.1
USER superset
