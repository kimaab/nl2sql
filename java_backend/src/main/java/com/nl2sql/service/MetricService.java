package com.nl2sql.service;

import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;
import java.util.function.BiFunction;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import com.nl2sql.common.ApiException;
import com.nl2sql.common.Dyn;
import com.nl2sql.common.Json;
import com.nl2sql.common.Py;
import com.nl2sql.common.Times;
import com.nl2sql.compiler.Compiler;
import com.nl2sql.db.MetaDb;
import com.nl2sql.db.TargetDb;
import com.nl2sql.db.TargetSession;
import com.nl2sql.model.MetricModels.Metric;
import com.nl2sql.model.MetricModels.MetricCheck;
import com.nl2sql.model.MetricModels.MetricHistoryEntry;
import com.nl2sql.model.MetricModels.MetricInput;
import com.nl2sql.sqlast.SqlAst;

/**
 * 지표 = 이름 · 설명 · 같은 말 + SQL. SQL 을 AST 로 옮겨 저장하고, 질문마다 LLM 이 그 AST 의 조건만 바꿔 쓴다 (metrics.py).
 *
 * 등록할 때 AST 를 다시 컴파일한 SQL 이 원본과 같은 결과를 내는지 대상 DB 에서 확인한다 — AST 로 못 옮기는 문법은
 * 변환에서 걸리고, 옮기며 뜻이 바뀌면(정수 나눗셈 등) 결과 비교에서 걸린다.
 */
@Service
public class MetricService {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.metrics");

    public static final int VERIFY_TIMEOUT_S = 60;
    static final int MAX_COMPARE_ROWS = 100_000;  // 해시로 비교하지 못하는 드라이버(MySQL·Oracle)에서 가져와 비교할 최대 행 수
    public static final Map<String, String> STAGE_LABEL = Map.of("parse", "AST 변환 실패", "compile", "컴파일 실패",
            "guard", "실행 전 확인에서 멈춤", "verify", "결과 비교 실패");

    private final MetaDb db;
    private final ContractService contracts;
    private final Connections connections;
    private final TargetDb targetDb;

    public MetricService(MetaDb db, ContractService contracts, Connections connections, TargetDb targetDb) {
        this.db = db;
        this.contracts = contracts;
        this.connections = connections;
        this.targetDb = targetDb;
    }

    public List<Metric> listMetrics(String systemId) {
        List<Map<String, Object>> rows = new ArrayList<>(contracts.loadMetricRows(systemId));
        Compiler compiler = compiler(systemId);
        rows.sort(Comparator.comparing((Map<String, Object> r) -> (OffsetDateTime) r.get("created_at")).reversed());
        List<Metric> metrics = new ArrayList<>();
        for (Map<String, Object> r : rows) {
            metrics.add(toMetric(r, compiler));
        }
        return metrics;
    }

    public Metric getMetric(String systemId, String metricId) {
        for (Map<String, Object> row : contracts.loadMetricRows(systemId)) {
            if (String.valueOf(row.get("id")).equals(metricId)) {
                return toMetric(row, compiler(systemId));
            }
        }
        throw new ApiException(404, "지표를 찾을 수 없습니다");
    }

    // ── 등록 전 확인: SQL → AST → SQL → 결과 비교 ───────────────────────────

    public MetricCheck checkMetric(String systemId, String sql) {
        return checkMetric(systemId, sql, null);
    }

    /**
     * 저장하지 않고 등록 절차만 밟아 본다. 실패하면 error 와 단계(stage)를 채워 돌려준다.
     *
     * guard(원본 SQL, 옮긴 SQL) 는 대상 DB 에서 실행하기 전에 부른다 — 실행하면 안 되는 이유를 돌려주면 실행하지 않는다.
     */
    public MetricCheck checkMetric(String systemId, String sql, BiFunction<String, String, String> guard) {
        Map<String, Object> contract = contracts.loadContract(systemId);
        SqlAst.Parsed parsed;
        try {
            parsed = SqlAst.parseSql(sql, contract);
        } catch (IllegalArgumentException error) {
            return MetricCheck.failed("parse", error.getMessage());
        }
        MetricCheck out = new MetricCheck();
        out.ast = parsed.ast();
        out.warnings = parsed.warnings();
        String compiled;
        try {
            compiled = new Compiler(contract).compile(parsed.ast());
        } catch (IllegalArgumentException error) {
            out.stage = "compile";
            out.error = error.getMessage();
            return out;
        }
        out.sql = compiled;
        out.tables = Compiler.astTables(parsed.ast());
        if (guard != null) {
            String reason = guard.apply(sql, compiled);
            if (reason != null && !reason.isEmpty()) {
                out.stage = "guard";
                out.error = reason;
                return out;
            }
        }
        try {
            out.rows = verify(systemId, sql, compiled);
        } catch (IllegalArgumentException error) {
            out.stage = "verify";
            out.error = error.getMessage();
        } catch (ApiException error) {
            out.stage = "verify";
            out.error = error.detail();
        }
        return out;
    }

    /**
     * 원본 SQL 과 AST 에서 다시 만든 SQL 을 대상 DB 에서 실행해 결과가 같은지 본다 → 행 수.
     *
     * 한 읽기 전용 트랜잭션의 같은 스냅샷에서 돌린다 — 데이터가 계속 쌓이는 테이블이어도 두 쿼리가 같은 데이터를 본다.
     * 행 순서는 보지 않는다 (ORDER BY 없는 결과는 순서가 정해지지 않는다).
     */
    public int verify(String systemId, String original, String compiled) {
        Map<String, Object> row = connections.row(systemId);
        String text = original.strip();
        while (text.endsWith(";")) {
            text = text.substring(0, text.length() - 1);
        }
        List<Object> a;
        List<Object> b;
        try (TargetSession cur = targetDb.readOnly(row, VERIFY_TIMEOUT_S)) {
            a = digest(cur, text, "원본");
            b = digest(cur, compiled, "옮긴");
            if (!a.equals(b) && !digest(cur, text, "원본").equals(a)) {
                throw new IllegalArgumentException("원본 SQL 의 결과가 실행할 때마다 달라 비교할 수 없습니다. "
                        + "ORDER BY 기준이 같은 행이 여럿이면(예: 같은 시각에 두 건) 어느 행이 뽑힐지 정해지지 않습니다 — "
                        + "ORDER BY 에 기준 컬럼을 더해 한 행으로 정해지게 하십시오.");
            }
        }
        if (!a.equals(b)) {
            throw new IllegalArgumentException("원본과 AST 로 옮긴 SQL 의 결과가 다릅니다 (원본 " + a.get(0) + "행, 옮긴 것 "
                    + b.get(0) + "행). 옮기며 뜻이 바뀌었습니다 — 예: 정수끼리 나누기(/)는 소수 나누기가 됩니다.");
        }
        return ((Number) a.get(0)).intValue();
    }

    /**
     * SQL 의 비교값.
     * PostgreSQL: [행 수, 결과 전체의 md5] 를 DB 안에서 만든다.
     * MySQL · Oracle: 행을 가져와 [행 수, 정렬한 행 목록] 으로 비교한다.
     */
    private static List<Object> digest(TargetSession cur, String sql, String which) {
        try {
            if (cur.driver().equals("postgresql")) {
                List<Object> r = cur.one("SELECT count(*), md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), '')) "
                        + "FROM (" + sql + ") t");
                return List.of(((Number) r.get(0)).longValue(), String.valueOf(r.get(1)));
            }
            List<List<Object>> rows = cur.query(sql, MAX_COMPARE_ROWS + 1).rows();
            if (rows.size() > MAX_COMPARE_ROWS) {
                throw new IllegalArgumentException("결과가 " + Py.comma(MAX_COMPARE_ROWS) + "행을 넘어 비교할 수 없습니다 — 기간을 좁혀 등록하십시오");
            }
            List<String> printed = new ArrayList<>();
            for (List<Object> r : rows) {
                printed.add(Objects.toString(r));
            }
            Collections.sort(printed);
            return List.of((long) rows.size(), printed);
        } catch (SQLException error) {
            cur.rollback();
            if ("57014".equals(error.getSQLState()) || error instanceof java.sql.SQLTimeoutException) {
                throw new IllegalArgumentException("결과 비교가 " + VERIFY_TIMEOUT_S + "초를 넘었습니다 — 기간을 좁혀 등록하십시오");
            }
            throw new IllegalArgumentException(which + " SQL 실행 실패: " + Py.firstLine(error));
        }
    }

    // ── 저장 ─────────────────────────────────────────────────────────────

    public Metric createMetric(String systemId, MetricInput data) {
        guardDuplicateName(systemId, data.name(), null);
        MetricCheck check = checked(systemId, data.sql());
        String metricId = UUID.randomUUID().toString();
        db.transaction(tx -> {
            Object[] values = values(data, check);
            tx.update("""
                    INSERT INTO metric (id, system_id, name, description, synonyms, source_sql, ast)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, metricId, systemId, values[0], values[1], values[2], values[3], values[4]);
            saveTables(tx, systemId, metricId, check.tables);
            recordHistory(tx, systemId, metricId, 1, "create");
        });
        log.info("metric created: {} ({})", metricId, data.name());
        return getMetric(systemId, metricId);
    }

    /** 지표 수정. 버전을 올리고 이전 정의는 이력에 남는다. */
    public Metric updateMetric(String systemId, String metricId, MetricInput data) {
        Metric current = getMetric(systemId, metricId);
        guardDuplicateName(systemId, data.name(), metricId);
        MetricCheck check = checked(systemId, data.sql());
        int version = current.version() + 1;
        db.transaction(tx -> {
            Object[] values = values(data, check);
            tx.update("""
                    UPDATE metric SET name = ?, description = ?, synonyms = ?, source_sql = ?, ast = ?,
                           status = 'active', broken_reason = NULL, version = ?, updated_at = now()
                     WHERE id = ? AND system_id = ?
                    """, values[0], values[1], values[2], values[3], values[4], version, metricId, systemId);
            saveTables(tx, systemId, metricId, check.tables);
            recordHistory(tx, systemId, metricId, version, "update");
        });
        log.info("metric updated: {} ({}, v{})", metricId, data.name(), version);
        return getMetric(systemId, metricId);
    }

    /** 지표 삭제. 마지막 정의는 이력에 남는다. */
    public void deleteMetric(String systemId, String metricId) {
        Map<String, Object> row = db.one("SELECT name, version FROM metric WHERE id = ? AND system_id = ?", metricId, systemId);
        if (row == null) {
            return;
        }
        db.transaction(tx -> {
            recordHistory(tx, systemId, metricId, ((Number) row.get("version")).intValue(), "delete");
            tx.update("DELETE FROM metric WHERE id = ? AND system_id = ?", metricId, systemId);
        });
        log.info("metric deleted: {}", metricId);
    }

    public List<MetricHistoryEntry> listMetricHistory(String systemId, String metricId) {
        List<MetricHistoryEntry> entries = new ArrayList<>();
        for (Map<String, Object> r : db.query("""
                SELECT version, action, snapshot, created_at FROM metric_history
                 WHERE system_id = ? AND metric_id = ? ORDER BY id DESC
                """, systemId, metricId)) {
            entries.add(new MetricHistoryEntry(((Number) r.get("version")).intValue(), (String) r.get("action"),
                    r.get("snapshot"), Times.iso((OffsetDateTime) r.get("created_at"))));
        }
        return entries;
    }

    /**
     * 동기화 뒤 지표 상태를 다시 매긴다: AST 가 지금 스키마로 컴파일되지 않으면 broken.
     *
     * 깨진 지표 [{name, reason}] 를 돌려준다.
     */
    public List<Map<String, Object>> refreshStatuses(String systemId) {
        Compiler compiler = compiler(systemId);
        List<Map<String, Object>> broken = new ArrayList<>();
        List<Object[]> updates = new ArrayList<>();
        for (Map<String, Object> r : contracts.loadMetricRows(systemId)) {
            String status;
            String reason;
            try {
                compiler.compile(r.get("ast"));
                status = "active";
                reason = null;
            } catch (IllegalArgumentException error) {
                status = "broken";
                reason = "스키마와 맞지 않습니다 — " + error.getMessage();
                broken.add(Dyn.of("name", r.get("name"), "reason", reason));
            }
            if (!status.equals(r.get("status")) || !Objects.equals(reason, r.get("broken_reason"))) {
                updates.add(new Object[] {status, reason, String.valueOf(r.get("id"))});
            }
        }
        if (!updates.isEmpty()) {
            db.transaction(tx -> tx.batch("UPDATE metric SET status = ?, broken_reason = ? WHERE id = ?", updates));
        }
        broken.sort(Comparator.comparing(b -> (String) b.get("name")));
        return broken;
    }

    private MetricCheck checked(String systemId, String sql) {
        MetricCheck check = checkMetric(systemId, sql);
        if (check.error != null) {
            throw new ApiException(400, STAGE_LABEL.getOrDefault(check.stage, "확인 실패") + ": " + check.error);
        }
        return check;
    }

    private static Object[] values(MetricInput data, MetricCheck check) {
        String sql = data.sql().strip();
        while (sql.endsWith(";")) {
            sql = sql.substring(0, sql.length() - 1);
        }
        return new Object[] {data.name(), data.description(), Json.dumps(data.synonyms()), sql.strip(), Json.dumps(check.ast)};
    }

    /** 지표가 쓰는 테이블. 질문 처리에서 테이블마다 지표 설명을 붙일 때 쓴다. */
    private void saveTables(MetaDb tx, String systemId, String metricId, List<String> names) {
        Map<String, Map<String, Object>> tables = contracts.loadTables(systemId);
        tx.update("DELETE FROM metric_table WHERE metric_id = ?", metricId);
        List<Object[]> links = new ArrayList<>();
        for (int i = 0; i < names.size(); i++) {
            Map<String, Object> table = tables.get(names.get(i));
            if (table != null) {
                links.add(new Object[] {metricId, table.get("id"), i == 0 ? "base" : "join"});
            }
        }
        tx.batch("INSERT INTO metric_table (metric_id, table_id, role) VALUES (?, ?, ?)", links);
    }

    private static void recordHistory(MetaDb tx, String systemId, String metricId, int version, String action) {
        tx.update("""
                INSERT INTO metric_history (system_id, metric_id, version, action, snapshot)
                SELECT m.system_id, m.id, ?, ?,
                       jsonb_build_object('name', m.name, 'description', m.description, 'synonyms', m.synonyms,
                                          'sql', m.source_sql, 'ast', m.ast)
                  FROM metric m
                 WHERE m.id = ? AND m.system_id = ?
                """, version, action, metricId, systemId);
    }

    private void guardDuplicateName(String systemId, String name, String exceptId) {
        Map<String, Object> existing = db.one("SELECT id FROM metric WHERE system_id = ? AND lower(name) = lower(?) AND id <> ?",
                systemId, name, exceptId == null ? UUID.randomUUID().toString() : exceptId);
        if (existing != null) {
            throw new ApiException(409, "'" + name + "' 이름의 지표가 이미 있습니다");
        }
    }

    Compiler compiler(String systemId) {
        Map<String, Object> row = db.one("SELECT driver FROM meta_system WHERE id = ?", systemId);
        Map<String, Map<String, Object>> tables = contracts.loadTables(systemId);
        return new Compiler(Dyn.of("driver", row != null ? row.get("driver") : "postgresql", "tables",
                new ArrayList<>(tables.values())));
    }

    private static Metric toMetric(Map<String, Object> row, Compiler compiler) {
        String compiled;
        try {
            compiled = compiler.compile(row.get("ast"));
        } catch (IllegalArgumentException error) {
            compiled = null;
        }
        return new Metric(
                String.valueOf(row.get("id")),
                (String) row.get("name"),
                (String) row.get("description"),
                Dyn.items(row.get("synonyms")),
                (String) row.get("source_sql"),
                row.get("ast"),
                compiled,
                Dyn.items(row.get("tables")),
                (String) row.get("status"),
                (String) row.get("broken_reason"),
                ((Number) row.get("version")).intValue(),
                row.get("updated_at") == null ? null : Times.iso((OffsetDateTime) row.get("updated_at")));
    }
}
