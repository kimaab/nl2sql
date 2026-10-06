import logging
from uuid import UUID, uuid4

import db
from contract import load_tables
from models import ApiException, Relation, RelationInput

log = logging.getLogger("nl2sql.relations")


def list_relations(datasource_id: UUID) -> list[Relation]:
    rows = db.query("""
        SELECT * FROM datasource_relation WHERE datasource_id = %s
         ORDER BY left_table, right_table, constraint_name, left_column
    """, str(datasource_id))
    return [_row_to_relation(r) for r in rows]


def create_relation(datasource_id: UUID, data: RelationInput) -> Relation:
    """사람이 관계를 등록한다. FK 가 없는 DB(뷰, 레거시 스키마)를 위한 것이다."""
    tables = load_tables(datasource_id)

    def resolve(table: str, column: str) -> tuple[str, str]:
        found = next((t for t in tables if t.lower() == table.lower()), None)
        if found is None:
            raise ApiException(400, f"테이블을 찾을 수 없습니다: {table}")
        col = next((c["name"] for c in tables[found]["columns"] if c["name"].lower() == column.lower()), None)
        if col is None:
            raise ApiException(400, f"컬럼을 찾을 수 없습니다: {found}.{column}")
        return found, col

    left_table, left_column = resolve(data.left_table, data.left_column)
    right_table, right_column = resolve(data.right_table, data.right_column)
    if left_table == right_table:
        raise ApiException(400, "같은 테이블끼리의 관계는 등록할 수 없습니다")

    duplicate = db.one("""
        SELECT id FROM datasource_relation
         WHERE datasource_id = %s AND (
               (left_table = %s AND left_column = %s AND right_table = %s AND right_column = %s)
            OR (left_table = %s AND left_column = %s AND right_table = %s AND right_column = %s))
    """, str(datasource_id), left_table, left_column, right_table, right_column,
        right_table, right_column, left_table, left_column)
    if duplicate:
        raise ApiException(409, "같은 관계가 이미 있습니다")

    relation_id = uuid4()
    constraint = data.constraint_name.strip() or f"{left_table}.{left_column}->{right_table}.{right_column}"
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO datasource_relation
                (id, datasource_id, constraint_name, left_table, left_column, right_table, right_column, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'manual')
            """, (str(relation_id), str(datasource_id), constraint, left_table, left_column, right_table, right_column))
    log.info("relation created: %s %s.%s -> %s.%s", relation_id, left_table, left_column, right_table, right_column)
    return _row_to_relation(db.one("SELECT * FROM datasource_relation WHERE id = %s", str(relation_id)))


def delete_relation(datasource_id: UUID, relation_id: UUID) -> None:
    row = db.one(
        "SELECT source FROM datasource_relation WHERE id = %s AND datasource_id = %s",
        str(relation_id), str(datasource_id),
    )
    if row is None:
        return
    if row["source"] == "fk":
        raise ApiException(400, "대상 DB의 FK 에서 읽은 관계는 다음 동기화가 다시 만듭니다. FK 를 지우거나 그대로 두십시오")
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM datasource_relation WHERE id = %s", (str(relation_id),))
    log.info("relation deleted: %s", relation_id)


def replace_fk_relations(cur, datasource_id: UUID, relations: list[dict]) -> None:
    """동기화 트랜잭션 안에서 FK 관계를 통째로 교체한다. 사람이 등록한 관계는 남긴다."""
    cur.execute("DELETE FROM datasource_relation WHERE datasource_id = %s AND source = 'fk'", (str(datasource_id),))
    if relations:
        cur.executemany("""
            INSERT INTO datasource_relation
            (id, datasource_id, constraint_name, left_table, left_column, right_table, right_column, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'fk')
        """, [
            (str(uuid4()), str(datasource_id), r["constraint"], r["left_table"], r["left_column"],
             r["right_table"], r["right_column"])
            for r in relations
        ])


def _row_to_relation(row: dict) -> Relation:
    return Relation(
        id=row["id"],
        constraint_name=row["constraint_name"],
        left_table=row["left_table"],
        left_column=row["left_column"],
        right_table=row["right_table"],
        right_column=row["right_column"],
        source=row["source"],
    )
