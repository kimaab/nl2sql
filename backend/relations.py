import logging
from uuid import UUID, uuid4

import db
from contract import load_tables
from models import ApiException, Relation, RelationInput

log = logging.getLogger("nl2sql.relations")

# 테이블은 id 로 들고, 화면·계약서에는 이름으로 낸다
_SELECT = """
    SELECT r.id, r.constraint_name, r.source, r.left_column, r.right_column,
           lt.name AS left_table, rt.name AS right_table
      FROM meta_relation r
      JOIN meta_table lt ON lt.id = r.left_table_id AND lt.deleted_at IS NULL
      JOIN meta_table rt ON rt.id = r.right_table_id AND rt.deleted_at IS NULL
"""


def list_relations(system_id: UUID) -> list[Relation]:
    rows = db.query(f"{_SELECT} WHERE r.system_id = %s ORDER BY lt.name, rt.name, r.constraint_name, r.left_column",
                    str(system_id))
    return [Relation(**r) for r in rows]


def create_relation(system_id: UUID, data: RelationInput) -> Relation:
    """사람이 관계를 등록한다. FK 가 없는 DB(뷰, 레거시 스키마)를 위한 것이다."""
    tables = load_tables(system_id)

    def resolve(table: str, column: str) -> tuple[dict, str]:
        found = next((t for t in tables.values() if t["name"].lower() == table.lower()), None)
        if found is None:
            raise ApiException(400, f"테이블을 찾을 수 없습니다: {table}")
        col = next((c["name"] for c in found["columns"] if c["name"].lower() == column.lower()), None)
        if col is None:
            raise ApiException(400, f"컬럼을 찾을 수 없습니다: {found['name']}.{column}")
        return found, col

    left, left_column = resolve(data.left_table, data.left_column)
    right, right_column = resolve(data.right_table, data.right_column)
    if left["id"] == right["id"]:
        raise ApiException(400, "같은 테이블끼리의 관계는 등록할 수 없습니다")

    duplicate = db.one("""
        SELECT id FROM meta_relation
         WHERE system_id = %s AND (
               (left_table_id = %s AND left_column = %s AND right_table_id = %s AND right_column = %s)
            OR (left_table_id = %s AND left_column = %s AND right_table_id = %s AND right_column = %s))
    """, str(system_id), left["id"], left_column, right["id"], right_column,
        right["id"], right_column, left["id"], left_column)
    if duplicate:
        raise ApiException(409, "같은 관계가 이미 있습니다")

    relation_id = uuid4()
    constraint = data.constraint_name.strip() or f"{left['name']}.{left_column}->{right['name']}.{right_column}"
    with db.connection() as conn:
        conn.execute("""
            INSERT INTO meta_relation (id, system_id, constraint_name, left_table_id, left_column,
                                       right_table_id, right_column, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'manual')
        """, (str(relation_id), str(system_id), constraint, left["id"], left_column, right["id"], right_column))
    log.info("relation created: %s %s.%s -> %s.%s", relation_id, left["name"], left_column, right["name"], right_column)
    return Relation(**db.one(f"{_SELECT} WHERE r.id = %s", str(relation_id)))


def delete_relation(system_id: UUID, relation_id: UUID) -> None:
    row = db.one("SELECT source FROM meta_relation WHERE id = %s AND system_id = %s", str(relation_id), str(system_id))
    if row is None:
        return
    if row["source"] == "fk":
        raise ApiException(400, "대상 DB의 FK 에서 읽은 관계는 다음 동기화가 다시 만듭니다. FK 를 지우거나 그대로 두십시오")
    with db.connection() as conn:
        conn.execute("DELETE FROM meta_relation WHERE id = %s", (str(relation_id),))
    log.info("relation deleted: %s", relation_id)


def replace_fk_relations(cur, system_id: UUID, relations: list[dict]) -> None:
    """동기화 트랜잭션 안에서 FK 관계를 통째로 교체한다. 사람이 등록한 관계는 남긴다.

    병합이 끝난 뒤에 부른다 — 테이블 이름을 방금 병합한 id 로 바꾼다.
    """
    cur.execute("DELETE FROM meta_relation WHERE system_id = %s AND source = 'fk'", (str(system_id),))
    if not relations:
        return
    cur.execute("SELECT name, id FROM meta_table WHERE system_id = %s AND deleted_at IS NULL", (str(system_id),))
    ids = dict(cur.fetchall())
    rows = [
        (str(uuid4()), str(system_id), r["constraint"], ids[r["left_table"]], r["left_column"],
         ids[r["right_table"]], r["right_column"])
        for r in relations if r["left_table"] in ids and r["right_table"] in ids
    ]
    cur.executemany("""
        INSERT INTO meta_relation (id, system_id, constraint_name, left_table_id, left_column,
                                   right_table_id, right_column, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'fk')
    """, rows)
