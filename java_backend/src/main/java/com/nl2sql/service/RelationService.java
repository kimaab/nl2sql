package com.nl2sql.service;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import com.nl2sql.common.ApiException;
import com.nl2sql.common.Dyn;
import com.nl2sql.db.MetaDb;
import com.nl2sql.model.MetricModels.Relation;
import com.nl2sql.model.MetricModels.RelationInput;

/** 테이블 관계. FK 는 동기화가(source='fk'), 나머지는 사람이(source='manual') 관리한다 (relations.py) */
@Service
public class RelationService {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.relations");

    // 테이블은 id 로 들고, 화면·계약서에는 이름으로 낸다
    private static final String SELECT = """
            SELECT r.id, r.constraint_name, r.source, r.left_column, r.right_column,
                   lt.name AS left_table, rt.name AS right_table
              FROM meta_relation r
              JOIN meta_table lt ON lt.id = r.left_table_id AND lt.deleted_at IS NULL
              JOIN meta_table rt ON rt.id = r.right_table_id AND rt.deleted_at IS NULL
            """;

    private final MetaDb db;
    private final ContractService contracts;

    public RelationService(MetaDb db, ContractService contracts) {
        this.db = db;
        this.contracts = contracts;
    }

    public List<Relation> listRelations(String systemId) {
        List<Relation> relations = new ArrayList<>();
        for (Map<String, Object> r : db.query(
                SELECT + " WHERE r.system_id = ? ORDER BY lt.name, rt.name, r.constraint_name, r.left_column", systemId)) {
            relations.add(toRelation(r));
        }
        return relations;
    }

    /** 사람이 관계를 등록한다. FK 가 없는 DB(뷰, 레거시 스키마)를 위한 것이다. */
    public Relation createRelation(String systemId, RelationInput data) {
        Map<String, Map<String, Object>> tables = contracts.loadTables(systemId);
        Object[] left = resolve(tables, data.leftTable(), data.leftColumn());
        Object[] right = resolve(tables, data.rightTable(), data.rightColumn());
        Map<String, Object> lt = Dyn.map(left[0]);
        Map<String, Object> rt = Dyn.map(right[0]);
        if (lt.get("id").equals(rt.get("id"))) {
            throw new ApiException(400, "같은 테이블끼리의 관계는 등록할 수 없습니다");
        }
        Map<String, Object> duplicate = db.one("""
                SELECT id FROM meta_relation
                 WHERE system_id = ? AND (
                       (left_table_id = ? AND left_column = ? AND right_table_id = ? AND right_column = ?)
                    OR (left_table_id = ? AND left_column = ? AND right_table_id = ? AND right_column = ?))
                """, systemId, lt.get("id"), left[1], rt.get("id"), right[1], rt.get("id"), right[1], lt.get("id"), left[1]);
        if (duplicate != null) {
            throw new ApiException(409, "같은 관계가 이미 있습니다");
        }
        String relationId = UUID.randomUUID().toString();
        String constraint = data.constraintName().strip();
        if (constraint.isEmpty()) {
            constraint = lt.get("name") + "." + left[1] + "->" + rt.get("name") + "." + right[1];
        }
        db.update("""
                INSERT INTO meta_relation (id, system_id, constraint_name, left_table_id, left_column,
                                           right_table_id, right_column, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'manual')
                """, relationId, systemId, constraint, lt.get("id"), left[1], rt.get("id"), right[1]);
        log.info("relation created: {} {}.{} -> {}.{}", relationId, lt.get("name"), left[1], rt.get("name"), right[1]);
        return toRelation(db.one(SELECT + " WHERE r.id = ?", relationId));
    }

    /** → [테이블, 실제 컬럼 이름] (대소문자 무시) */
    private static Object[] resolve(Map<String, Map<String, Object>> tables, String table, String column) {
        Map<String, Object> found = null;
        for (Map<String, Object> t : tables.values()) {
            if (((String) t.get("name")).equalsIgnoreCase(table)) {
                found = t;
                break;
            }
        }
        if (found == null) {
            throw new ApiException(400, "테이블을 찾을 수 없습니다: " + table);
        }
        String col = null;
        for (Map<String, Object> c : Dyn.maps(found.get("columns"))) {
            if (((String) c.get("name")).equalsIgnoreCase(column)) {
                col = (String) c.get("name");
                break;
            }
        }
        if (col == null) {
            throw new ApiException(400, "컬럼을 찾을 수 없습니다: " + found.get("name") + "." + column);
        }
        return new Object[] {found, col};
    }

    public void deleteRelation(String systemId, String relationId) {
        Map<String, Object> row = db.one("SELECT source FROM meta_relation WHERE id = ? AND system_id = ?", relationId, systemId);
        if (row == null) {
            return;
        }
        if ("fk".equals(row.get("source"))) {
            throw new ApiException(400, "대상 DB의 FK 에서 읽은 관계는 다음 동기화가 다시 만듭니다. FK 를 지우거나 그대로 두십시오");
        }
        db.update("DELETE FROM meta_relation WHERE id = ?", relationId);
        log.info("relation deleted: {}", relationId);
    }

    /**
     * 동기화 트랜잭션 안에서 FK 관계를 통째로 교체한다. 사람이 등록한 관계는 남긴다.
     *
     * 병합이 끝난 뒤에 부른다 — 테이블 이름을 방금 병합한 id 로 바꾼다.
     */
    public void replaceFkRelations(MetaDb tx, String systemId, List<Map<String, Object>> relations) {
        tx.update("DELETE FROM meta_relation WHERE system_id = ? AND source = 'fk'", systemId);
        if (relations.isEmpty()) {
            return;
        }
        Map<String, Object> ids = new java.util.HashMap<>();
        for (Map<String, Object> r : tx.query("SELECT name, id FROM meta_table WHERE system_id = ? AND deleted_at IS NULL", systemId)) {
            ids.put((String) r.get("name"), r.get("id"));
        }
        List<Object[]> rows = new ArrayList<>();
        for (Map<String, Object> r : relations) {
            if (!ids.containsKey(r.get("left_table")) || !ids.containsKey(r.get("right_table"))) {
                continue;
            }
            rows.add(new Object[] {UUID.randomUUID().toString(), systemId, r.get("constraint"), ids.get(r.get("left_table")),
                    r.get("left_column"), ids.get(r.get("right_table")), r.get("right_column")});
        }
        tx.batch("""
                INSERT INTO meta_relation (id, system_id, constraint_name, left_table_id, left_column,
                                           right_table_id, right_column, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'fk')
                """, rows);
        // FK 가 생기면 같은 관계를 추론해 둔 auto 관계는 지운다 (DescribeService)
        tx.update("""
                DELETE FROM meta_relation a USING meta_relation f
                 WHERE a.system_id = ? AND a.source = 'auto' AND f.system_id = a.system_id AND f.source = 'fk'
                   AND ((a.left_table_id = f.left_table_id AND a.left_column = f.left_column
                         AND a.right_table_id = f.right_table_id AND a.right_column = f.right_column)
                     OR (a.left_table_id = f.right_table_id AND a.left_column = f.right_column
                         AND a.right_table_id = f.left_table_id AND a.right_column = f.left_column))
                """, systemId);
    }

    private static Relation toRelation(Map<String, Object> r) {
        return new Relation((String) r.get("left_table"), (String) r.get("left_column"), (String) r.get("right_table"),
                (String) r.get("right_column"), (String) r.get("constraint_name"), String.valueOf(r.get("id")),
                (String) r.get("source"));
    }
}
