package com.nl2sql.service;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import com.nl2sql.common.ApiException;
import com.nl2sql.common.Dyn;
import com.nl2sql.common.Json;
import com.nl2sql.common.Py;
import com.nl2sql.common.Times;
import com.nl2sql.compiler.Compiler;
import com.nl2sql.db.Catalog;
import com.nl2sql.db.MetaDb;
import com.nl2sql.db.Secret;
import com.nl2sql.model.SystemModels.ColumnInfo;
import com.nl2sql.model.SystemModels.SyncLog;
import com.nl2sql.model.SystemModels.SyncResult;
import com.nl2sql.model.SystemModels.SystemInfo;
import com.nl2sql.model.SystemModels.SystemInput;
import com.nl2sql.model.SystemModels.TableInfo;
import com.nl2sql.model.SystemModels.TimeColumn;

/** 시스템(대상 DB) 등록 · 스키마 동기화 · 테이블 설정 (systems.py) */
@Service
public class SystemService {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.systems");

    private static final String COUNTS = """
            (SELECT count(*) FROM meta_table t WHERE t.system_id = s.id AND t.deleted_at IS NULL) AS table_count,
            (SELECT count(*) FROM metric m WHERE m.system_id = s.id) AS metric_count,
            (SELECT count(*) FROM metric m WHERE m.system_id = s.id AND m.status = 'active') AS active_metric_count
            """;

    private final MetaDb db;
    private final Secret secret;
    private final Connections connections;
    private final Catalog catalog;
    private final RelationService relations;
    private final MetricService metrics;

    public SystemService(MetaDb db, Secret secret, Connections connections, Catalog catalog, RelationService relations,
            MetricService metrics) {
        this.db = db;
        this.secret = secret;
        this.connections = connections;
        this.catalog = catalog;
        this.relations = relations;
        this.metrics = metrics;
    }

    private void guardDuplicate(SystemInput data, String exceptId) {
        Map<String, Object> row = db.one(
                "SELECT code, name FROM meta_system WHERE (lower(name) = lower(?) OR code = ?) AND id <> ?",
                data.name(), data.code(), exceptId == null ? UUID.randomUUID().toString() : exceptId);
        if (row != null) {
            String what = data.code().equals(row.get("code")) ? "코드" : "이름";
            throw new ApiException(409, "같은 " + what + "의 시스템이 이미 있습니다");
        }
    }

    public List<SystemInfo> listSystems() {
        List<SystemInfo> systems = new ArrayList<>();
        for (Map<String, Object> r : db.query("SELECT s.*, " + COUNTS + " FROM meta_system s ORDER BY s.name")) {
            systems.add(toSystem(r));
        }
        return systems;
    }

    public SystemInfo getSystem(String systemId) {
        Map<String, Object> row = db.one("SELECT s.*, " + COUNTS + " FROM meta_system s WHERE s.id = ?", uuid(systemId));
        if (row == null) {
            throw new ApiException(404, "시스템을 찾을 수 없습니다");
        }
        return toSystem(row);
    }

    public SystemInfo createSystem(SystemInput data) {
        guardDuplicate(data, null);
        String systemId = UUID.randomUUID().toString();
        db.update("""
                INSERT INTO meta_system (id, code, name, domain_desc, driver, host, port, db_name, db_schemas,
                                         username, password_enc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, systemId, data.code(), data.name(), data.domainDesc(), data.driver(), data.host(), data.port(),
                data.dbName(), MetaDb.array(data.dbSchemas()), data.username(), secret.encrypt(data.password()));
        log.info("system created: {} ({})", systemId, data.code());
        return getSystem(systemId);
    }

    public SystemInfo updateSystem(String systemId, SystemInput data) {
        getSystem(systemId);
        guardDuplicate(data, systemId);
        db.update("""
                UPDATE meta_system SET code = ?, name = ?, domain_desc = ?, driver = ?, host = ?, port = ?,
                       db_name = ?, db_schemas = ?, username = ?,
                       password_enc = COALESCE(?, password_enc), updated_at = now()
                 WHERE id = ?
                """, data.code(), data.name(), data.domainDesc(), data.driver(), data.host(), data.port(), data.dbName(),
                MetaDb.array(data.dbSchemas()), data.username(),
                data.password().isEmpty() ? null : secret.encrypt(data.password()), systemId);
        log.info("system updated: {}", systemId);
        return getSystem(systemId);
    }

    public void deleteSystem(String systemId) {
        db.update("DELETE FROM meta_system WHERE id = ?", uuid(systemId));
        log.info("system deleted: {}", systemId);
    }

    // ── 동기화 ───────────────────────────────────────────────────────────

    public SyncResult syncSystem(String systemId) {
        return syncSystem(systemId, "manual");
    }

    /** 스키마 동기화. 대상 DB 에 접속하는 유일한 지점. 성공·실패 모두 동기화 기록에 남는다. */
    public SyncResult syncSystem(String systemId, String trigger) {
        Map<String, Object> row = connections.row(uuid(systemId));
        OffsetDateTime started = OffsetDateTime.now(ZoneOffset.UTC);
        SyncResult result;
        try {
            Catalog.Read read = catalog.readCatalog(row);
            result = mergeSchema(systemId, read.tables(), read.relations());
        } catch (ApiException error) {
            writeSyncLog(systemId, trigger, started, null, error.detail());
            throw error;
        } catch (RuntimeException error) {
            writeSyncLog(systemId, trigger, started, null, Py.firstLine(error));
            throw error;
        }

        result = result.withBroken(metrics.refreshStatuses(systemId));
        for (Map<String, Object> broken : result.brokenMetrics()) {
            log.warn("동기화 뒤 깨진 지표: {} / {} — {}", row.get("name"), broken.get("name"), broken.get("reason"));
        }
        writeSyncLog(systemId, trigger, started, result, null);
        return result;
    }

    /**
     * 이름 기준 병합. 있으면 UPDATE(id 유지), 없으면 INSERT, 사라지면 deleted_at.
     *
     * 전체를 한 트랜잭션으로 처리한다 — 질문 도중 반쯤 바뀐 스키마를 보지 않게.
     */
    private SyncResult mergeSchema(String systemId, List<Map<String, Object>> tables, List<Map<String, Object>> fks) {
        List<Object> added = new ArrayList<>();
        List<Object> changed = new ArrayList<>();
        int[] columnCount = {0};
        int[] removed = {0};
        OffsetDateTime[] synced = {null};
        db.transaction(tx -> {
            Map<String, Object[]> existing = new LinkedHashMap<>();
            for (Map<String, Object> r : tx.query("SELECT id, name, deleted_at FROM meta_table WHERE system_id = ?", systemId)) {
                existing.put((String) r.get("name"), new Object[] {r.get("id"), r.get("deleted_at")});
            }
            Map<String, Set<String>> before = new LinkedHashMap<>();
            for (Map<String, Object> r : tx.query("""
                    SELECT c.table_id, c.name, c.data_type FROM meta_column c JOIN meta_table t ON t.id = c.table_id
                     WHERE t.system_id = ? AND c.deleted_at IS NULL
                    """, systemId)) {
                before.computeIfAbsent(String.valueOf(r.get("table_id")), k -> new HashSet<>())
                        .add(r.get("name") + "\u0000" + r.get("data_type"));
            }

            for (Map<String, Object> table : tables) {
                String name = (String) table.get("name");
                String tid;
                if (existing.containsKey(name)) {
                    Object[] found = existing.get(name);
                    tid = String.valueOf(found[0]);
                    tx.update("""
                            UPDATE meta_table SET comment = ?, db_schema = ?, deleted_at = NULL, synced_at = now(),
                                                  updated_at = now()
                             WHERE id = ?
                            """, table.get("description"), table.get("schema"), tid);
                    Set<String> nowCols = new HashSet<>();
                    for (Map<String, Object> c : Dyn.maps(table.get("columns"))) {
                        nowCols.add(c.get("name") + "\u0000" + c.get("type"));
                    }
                    if (found[1] != null) {
                        added.add(tid);
                    } else if (!before.getOrDefault(tid, Set.of()).equals(nowCols)) {
                        changed.add(tid);
                    }
                } else {
                    tid = UUID.randomUUID().toString();
                    tx.update("INSERT INTO meta_table (id, system_id, name, db_schema, comment) VALUES (?, ?, ?, ?, ?)",
                            tid, systemId, name, table.get("schema"), table.get("description"));
                    added.add(tid);
                }

                List<String> names = new ArrayList<>();
                List<Object[]> columnRows = new ArrayList<>();
                int i = 0;
                for (Map<String, Object> c : Dyn.maps(table.get("columns"))) {
                    names.add((String) c.get("name"));
                    columnRows.add(new Object[] {tid, c.get("name"), c.get("type"), c.get("description"), i,
                            Py.truthy(c.get("is_pk"))});
                    i += 1;
                }
                tx.batch("""
                        INSERT INTO meta_column (table_id, name, data_type, comment, ordinal, is_pk)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT (table_id, name) DO UPDATE SET
                            data_type = EXCLUDED.data_type, comment = EXCLUDED.comment, ordinal = EXCLUDED.ordinal,
                            is_pk = EXCLUDED.is_pk, deleted_at = NULL, updated_at = now()
                        """, columnRows);
                tx.update("""
                        UPDATE meta_column SET deleted_at = now(), updated_at = now()
                         WHERE table_id = ? AND deleted_at IS NULL AND NOT (name = ANY(?::text[]))
                        """, tid, MetaDb.array(names));
                columnCount[0] += names.size();
            }

            List<String> tableNames = new ArrayList<>();
            for (Map<String, Object> t : tables) {
                tableNames.add((String) t.get("name"));
            }
            removed[0] = tx.query("""
                    UPDATE meta_table SET deleted_at = now(), updated_at = now()
                     WHERE system_id = ? AND deleted_at IS NULL AND NOT (name = ANY(?::text[]))
                    RETURNING id
                    """, systemId, MetaDb.array(tableNames)).size();

            if (fks != null) {
                relations.replaceFkRelations(tx, systemId, fks);
            }
            synced[0] = (OffsetDateTime) tx.one(
                    "UPDATE meta_system SET synced_at = now(), updated_at = now() WHERE id = ? RETURNING synced_at",
                    systemId).get("synced_at");
        });

        log.info("system {} synced: {} tables (+{} ~{} -{}), {} columns", systemId, tables.size(), added.size(),
                changed.size(), removed[0], columnCount[0]);
        return new SyncResult(tables.size(), columnCount[0], added.size(), changed.size(), removed[0],
                Times.iso(synced[0]), fks != null ? fks.size() : null, new ArrayList<>());
    }

    private void writeSyncLog(String systemId, String trigger, OffsetDateTime started, SyncResult result, String error) {
        db.update("""
                INSERT INTO meta_sync_log (system_id, trigger, status, tables_added, tables_changed, tables_removed,
                                           broken_metrics, error, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, systemId, trigger, error != null ? "error" : "ok",
                result != null ? result.tablesAdded() : null,
                result != null ? result.tablesChanged() : null,
                result != null ? result.tablesRemoved() : null,
                Json.dumps(result != null ? result.brokenMetrics() : List.of()), error, started);
    }

    public List<SyncLog> listSyncLogs(String systemId, int limit) {
        List<SyncLog> logs = new ArrayList<>();
        for (Map<String, Object> r : db.query("""
                SELECT l.*, s.name AS system_name
                  FROM meta_sync_log l JOIN meta_system s ON s.id = l.system_id
                 WHERE (?::uuid IS NULL OR l.system_id = ?::uuid)
                 ORDER BY l.started_at DESC LIMIT ?
                """, systemId, systemId, limit)) {
            logs.add(new SyncLog(
                    ((Number) r.get("id")).longValue(),
                    String.valueOf(r.get("system_id")),
                    (String) r.get("system_name"),
                    (String) r.get("trigger"),
                    (String) r.get("status"),
                    (Integer) r.get("tables_added"),
                    (Integer) r.get("tables_changed"),
                    (Integer) r.get("tables_removed"),
                    Dyn.items(r.get("broken_metrics")),
                    (String) r.get("error"),
                    Times.iso((OffsetDateTime) r.get("started_at")),
                    Times.iso((OffsetDateTime) r.get("finished_at"))));
        }
        return logs;
    }

    // ── 테이블 정보 ───────────────────────────────────────────────────────

    /** 저장된 스키마 (삭제 표시된 테이블·컬럼 제외) */
    public Map<String, Object> getSchema(String systemId) {
        Map<String, Map<String, Object>> tables = new LinkedHashMap<>();
        for (Map<String, Object> r : db.query("""
                SELECT t.name AS table_name, t.comment AS table_note, t.purpose,
                       c.name AS column_name, c.data_type, c.comment AS column_note, c.note AS column_human
                  FROM meta_table t
                  LEFT JOIN meta_column c ON c.table_id = t.id AND c.deleted_at IS NULL
                 WHERE t.system_id = ? AND t.deleted_at IS NULL
                 ORDER BY t.name, c.ordinal
                """, systemId)) {
            String name = (String) r.get("table_name");
            Map<String, Object> table = tables.computeIfAbsent(name, k -> Dyn.of("name", name, "description",
                    r.get("table_note"), "purpose", r.get("purpose"), "columns", new ArrayList<>()));
            if (r.get("column_name") != null) {
                Dyn.items(table.get("columns")).add(Dyn.of("name", r.get("column_name"), "type", r.get("data_type"),
                        "description", Py.truthy(r.get("column_human")) ? r.get("column_human") : r.get("column_note")));
            }
        }
        return Dyn.of("tables", new ArrayList<>(tables.values()));
    }

    public List<TableInfo> listTables(String systemId) {
        Map<String, List<TimeColumn>> times = timeColumns(systemId);
        Map<String, List<ColumnInfo>> columns = columns(systemId);
        List<TableInfo> tables = new ArrayList<>();
        for (Map<String, Object> r : db.query("""
                SELECT t.*,
                       (SELECT count(*) FROM meta_column c WHERE c.table_id = t.id AND c.deleted_at IS NULL) AS column_count,
                       (SELECT count(*) FROM metric_table mt WHERE mt.table_id = t.id) AS metric_count
                  FROM meta_table t WHERE t.system_id = ? AND t.deleted_at IS NULL ORDER BY t.name
                """, systemId)) {
            String id = String.valueOf(r.get("id"));
            tables.add(new TableInfo(id, (String) r.get("name"), (String) r.get("comment"), (String) r.get("ai_comment"),
                    (String) r.get("purpose"), ((Number) r.get("column_count")).intValue(),
                    ((Number) r.get("metric_count")).intValue(), times.getOrDefault(id, List.of()),
                    columns.getOrDefault(id, List.of())));
        }
        return tables;
    }

    private TableInfo findTable(String systemId, String tableId) {
        for (TableInfo t : listTables(systemId)) {
            if (t.id().equals(tableId)) {
                return t;
            }
        }
        throw new ApiException(404, "테이블을 찾을 수 없습니다");
    }

    /** 테이블별 컬럼과 '계산 필요' 표시 */
    private Map<String, List<ColumnInfo>> columns(String systemId) {
        Map<String, List<ColumnInfo>> out = new LinkedHashMap<>();
        for (Map<String, Object> r : db.query("""
                SELECT c.table_id, c.name, c.data_type, c.comment, c.ai_comment, c.note, c.needs_calc
                  FROM meta_column c JOIN meta_table t ON t.id = c.table_id
                 WHERE t.system_id = ? AND t.deleted_at IS NULL AND c.deleted_at IS NULL
                 ORDER BY c.table_id, c.ordinal
                """, systemId)) {
            out.computeIfAbsent(String.valueOf(r.get("table_id")), k -> new ArrayList<>()).add(new ColumnInfo(
                    (String) r.get("name"), (String) r.get("data_type"), (String) r.get("comment"),
                    (String) r.get("ai_comment"), (String) r.get("note"), Boolean.TRUE.equals(r.get("needs_calc"))));
        }
        return out;
    }

    /** 테이블별 시각 컬럼 (UTC 보정값을 적을 수 있는 컬럼). 날짜뿐인 컬럼은 시차가 뜻이 없어 뺀다. */
    private Map<String, List<TimeColumn>> timeColumns(String systemId) {
        Map<String, List<TimeColumn>> out = new LinkedHashMap<>();
        for (Map<String, Object> r : db.query("""
                SELECT c.table_id, c.name, c.data_type, c.utc_offset_hours, s.driver
                  FROM meta_column c
                  JOIN meta_table t ON t.id = c.table_id
                  JOIN meta_system s ON s.id = t.system_id
                 WHERE t.system_id = ? AND t.deleted_at IS NULL AND c.deleted_at IS NULL
                 ORDER BY c.table_id, c.ordinal
                """, systemId)) {
            if ("datetime".equals(Compiler.temporalKind((String) r.get("data_type"), (String) r.get("driver")))) {
                Object hours = r.get("utc_offset_hours");
                out.computeIfAbsent(String.valueOf(r.get("table_id")), k -> new ArrayList<>()).add(new TimeColumn(
                        (String) r.get("name"), (String) r.get("data_type"),
                        hours == null ? null : ((Number) hours).intValue()));
            }
        }
        return out;
    }

    public TableInfo setUtcOffset(String systemId, String tableId, String column, Integer hours) {
        TableInfo found = findTable(systemId, tableId);
        boolean timeColumn = false;
        for (TimeColumn c : found.timeColumns()) {
            timeColumn |= c.name().equals(column);
        }
        if (!timeColumn) {
            throw new ApiException(400, column + " 은(는) 시각을 포함한 날짜 컬럼이 아니라 UTC 보정을 쓸 수 없습니다");
        }
        db.update("""
                UPDATE meta_column SET utc_offset_hours = ?, updated_at = now()
                 WHERE table_id = ? AND name = ? AND deleted_at IS NULL
                """, hours, tableId, column);
        log.info("utc offset {}.{} = {}", found.name(), column, hours);
        return findTable(systemId, tableId);
    }

    public TableInfo setNeedsCalc(String systemId, String tableId, String column, boolean needsCalc) {
        TableInfo found = findTable(systemId, tableId);
        requireColumn(found, column);
        db.update("""
                UPDATE meta_column SET needs_calc = ?, updated_at = now()
                 WHERE table_id = ? AND name = ? AND deleted_at IS NULL
                """, needsCalc, tableId, column);
        log.info("needs_calc {}.{} = {}", found.name(), column, needsCalc);
        return findTable(systemId, tableId);
    }

    public TableInfo setColumnNote(String systemId, String tableId, String column, String note) {
        TableInfo found = findTable(systemId, tableId);
        requireColumn(found, column);
        db.update("""
                UPDATE meta_column SET note = ?, updated_at = now()
                 WHERE table_id = ? AND name = ? AND deleted_at IS NULL
                """, note.strip(), tableId, column);
        log.info("column note {}.{}", found.name(), column);
        return findTable(systemId, tableId);
    }

    public TableInfo setTablePurpose(String systemId, String tableId, String purpose) {
        String text = purpose == null ? "" : purpose.strip();
        db.update("""
                UPDATE meta_table SET purpose = ?, updated_at = now()
                 WHERE id = ? AND system_id = ? AND deleted_at IS NULL AND purpose IS DISTINCT FROM ?
                """, text, tableId, systemId, text);
        return findTable(systemId, tableId);
    }

    private static void requireColumn(TableInfo table, String column) {
        for (ColumnInfo c : table.columns()) {
            if (Objects.equals(c.name(), column)) {
                return;
            }
        }
        throw new ApiException(400, table.name() + " 에 없는 컬럼입니다 -> " + column);
    }

    private static SystemInfo toSystem(Map<String, Object> row) {
        Object supersetId = row.get("superset_database_id");
        return new SystemInfo(
                String.valueOf(row.get("id")),
                (String) row.get("code"),
                (String) row.get("name"),
                (String) row.get("domain_desc"),
                (String) row.get("driver"),
                (String) row.get("host"),
                ((Number) row.get("port")).intValue(),
                (String) row.get("db_name"),
                Dyn.strings(row.get("db_schemas")),
                (String) row.get("username"),
                Times.iso((OffsetDateTime) row.get("synced_at")),
                ((Number) row.get("table_count")).intValue(),
                ((Number) row.get("metric_count")).intValue(),
                ((Number) row.get("active_metric_count")).intValue(),
                supersetId == null ? null : ((Number) supersetId).intValue());
    }

    /** 경로의 id 가 UUID 모양인지 본다 (아니면 FastAPI 처럼 422) */
    public static String uuid(String id) {
        try {
            return UUID.fromString(id).toString();
        } catch (IllegalArgumentException error) {
            throw new ApiException(422, "id 는 UUID 여야 합니다 -> " + id);
        }
    }
}
