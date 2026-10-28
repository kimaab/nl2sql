package com.nl2sql.service;

import java.math.BigDecimal;
import java.sql.SQLException;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
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
import com.nl2sql.compiler.Dialect;
import com.nl2sql.db.MetaDb;
import com.nl2sql.db.TargetDb;
import com.nl2sql.db.TargetSession;
import com.nl2sql.llm.Llm;
import com.nl2sql.llm.LlmCalls;
import com.nl2sql.model.RunModels.DescribeRequest;
import com.nl2sql.model.RunModels.DescribeRun;

/**
 * 테이블 · 컬럼 설명 자동 채우기와 테이블 관계 추론 (describe.py).
 *
 * 설명: 대상 DB 코멘트가 있으면 그대로 쓴다. 없으면 LLM 이 이름 · 타입 · 샘플 값 몇 행을 보고 추론해 ai_comment 에 둔다.
 *       동기화는 ai_comment 를 건드리지 않고, 계약서(ContractService)는 코멘트가 비었을 때 ai_comment 를 쓴다.
 * 관계: FK 가 없는 DB 를 위해 컬럼 이름 · 타입 · PK 로 조인 관계를 추론해 source='auto' 로 둔다.
 *       다시 돌리면 auto 관계를 통째로 새로 만든다. FK · 사람이 등록한 관계와 겹치는 것은 만들지 않는다.
 */
@Service
public class DescribeService {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.describe");

    static final int SAMPLE_ROWS = 5;
    static final int SAMPLE_TIMEOUT_S = 15;
    static final int VALUE_CHARS = 40;  // 샘플 값 하나를 프롬프트에 실을 때 자르는 길이
    static final int DESC_CHARS = 200;  // 추론한 설명 하나의 최대 길이

    // 이 이름의 PK 는 어느 테이블에나 있어 이름만으로 가리키는 테이블을 알 수 없다 — '<테이블>_id' 처럼 적힌 컬럼만 잇는다
    static final Set<String> GENERIC_KEYS = Set.of("id", "seq", "no", "idx", "sn", "key", "code", "cd");
    static final List<String> KEY_SUFFIXES = List.of("_id", "_no", "_seq", "_sn", "_idx", "_key", "_code", "_cd");

    static final String SYSTEM_PROMPT = """
            너는 데이터베이스 메타데이터 작성자다.
            테이블 하나의 이름, 컬럼 이름 · 타입, 이미 있는 설명, 샘플 값 몇 행을 보고 '설명이 필요한 것'만 한국어로 쓴다.

            규칙:
            - 설명은 한 문장, 40자 안팎. 무엇을 담는지 쓴다 (예: "차량 식별 번호", "경고 발생 시각 (UTC)", "운행 상태 코드 (R=운행, S=정지)").
            - 코드 값 컬럼은 샘플에 보이는 값의 뜻을 짐작해 괄호에 적는다. 확실하지 않은 값의 뜻은 지어내지 않는다.
            - 단위 · 시간대가 이름이나 값에서 드러나면 적는다.
            - 이름 · 값으로 뜻을 전혀 알 수 없으면 빈 문자열로 둔다.
            - 설명이 필요하다고 한 것만 답한다.

            답은 JSON 객체 하나: {"table": "테이블 설명 (필요할 때만)", "columns": {"컬럼 이름": "설명"}}""";

    private final MetaDb db;
    private final Background background;
    private final ContractService contracts;
    private final Connections connections;
    private final TargetDb targetDb;

    public DescribeService(MetaDb db, Background background, ContractService contracts, Connections connections,
            TargetDb targetDb) {
        this.db = db;
        this.background = background;
        this.contracts = contracts;
        this.connections = connections;
        this.targetDb = targetDb;
    }

    // ── 실행 ──────────────────────────────────────────────────────────────

    /** 실행 기록을 만들고 스레드에서 돌린다. 진행은 getDescribeRun 으로 본다. */
    public DescribeRun startDescribe(String systemId, DescribeRequest req, Llm model) {
        if (!req.descriptions() && !req.relations()) {
            throw new ApiException(400, "설명과 관계 중 하나는 골라야 합니다");
        }
        List<Map<String, Object>> targets = new ArrayList<>();
        if (req.descriptions()) {
            targets = describeTargets(systemId, req.tableIds(), req.overwrite());
        }
        String runId = UUID.randomUUID().toString();
        Map<String, Object> options = Dyn.of("descriptions", req.descriptions(), "overwrite", req.overwrite(),
                "relations", req.relations(), "picked", req.tableIds().isEmpty() ? "auto" : "manual");
        db.update("INSERT INTO meta_describe_run (id, system_id, options, tables_total) VALUES (?, ?, ?, ?)",
                runId, systemId, Json.dumps(options), targets.size());

        List<Map<String, Object>> work = targets;
        boolean started = background.start("describe:" + systemId, runId,
                () -> runDescribe(systemId, runId, work, req.relations(), model),
                error -> close(runId, "error", Py.firstLine(error)));
        if (!started) {
            db.update("DELETE FROM meta_describe_run WHERE id = ?", runId);
            throw new ApiException(409, "이 시스템에서 설명 자동 채우기가 이미 돌고 있습니다");
        }
        log.info("설명 자동 채우기 시작: run={} tables={} relations={}", runId, targets.size(), req.relations());
        return getDescribeRun(systemId, runId);
    }

    void runDescribe(String systemId, String runId, List<Map<String, Object>> targets, boolean relations, Llm model) {
        if (relations) {
            int added = inferAndStoreRelations(systemId);
            db.update("UPDATE meta_describe_run SET relations_added = ? WHERE id = ?", added, runId);
        }
        if (!targets.isEmpty()) {
            Map<String, Object> row = connections.row(systemId);
            Dialect d = new Dialect((String) row.get("driver"));
            for (Map<String, Object> target : targets) {
                try {
                    int[] filled = describeTable(row, d, target, model);
                    db.update("""
                            UPDATE meta_describe_run SET tables_done = tables_done + 1,
                                   tables_filled = tables_filled + ?, columns_filled = columns_filled + ?
                             WHERE id = ?
                            """, filled[0], filled[1], runId);
                } catch (RuntimeException error) {
                    log.warn("{} 설명을 채우지 못했습니다: {}", target.get("name"), Py.firstLine(error));
                    String failure = Json.dumps(List.of(Dyn.of("table", target.get("name"), "error", Py.firstLine(error))));
                    db.update("UPDATE meta_describe_run SET tables_done = tables_done + 1, failures = failures || ?::jsonb"
                            + " WHERE id = ?", failure, runId);
                }
            }
        }
        close(runId, "done", null);
        log.info("설명 자동 채우기 끝: run={}", runId);
    }

    public List<DescribeRun> listDescribeRuns(String systemId, int limit) {
        List<DescribeRun> runs = new ArrayList<>();
        for (Map<String, Object> r : db.query(
                "SELECT * FROM meta_describe_run WHERE system_id = ? ORDER BY started_at DESC LIMIT ?", systemId, limit)) {
            runs.add(toRun(r));
        }
        return runs;
    }

    public DescribeRun getDescribeRun(String systemId, String runId) {
        Map<String, Object> row = db.one("SELECT * FROM meta_describe_run WHERE id = ? AND system_id = ?", runId, systemId);
        if (row == null) {
            throw new ApiException(404, "설명 자동 채우기 실행을 찾을 수 없습니다");
        }
        return toRun(row);
    }

    // ── 설명 ──────────────────────────────────────────────────────────────

    /**
     * 설명을 채울 테이블 → [{id, name, schema, comment, need_table, columns: [{name, type, comment, need}]}]
     *
     * 대상 DB 코멘트가 있는 것은 채우지 않는다. 전에 추론한 설명은 overwrite 일 때만 다시 채운다.
     */
    public List<Map<String, Object>> describeTargets(String systemId, List<String> tableIds, boolean overwrite) {
        Set<String> picked = new HashSet<>();
        for (String tid : tableIds) {
            picked.add(SystemService.uuid(tid));
        }
        Map<String, Map<String, Object>> tables = new LinkedHashMap<>();
        for (Map<String, Object> r : db.query("""
                SELECT t.id AS table_id, t.name AS table_name, t.db_schema, t.comment AS table_comment,
                       t.ai_comment AS table_ai, c.name AS column_name, c.data_type, c.comment, c.ai_comment, c.note, c.is_pk
                  FROM meta_table t
                  JOIN meta_column c ON c.table_id = t.id AND c.deleted_at IS NULL
                 WHERE t.system_id = ? AND t.deleted_at IS NULL
                 ORDER BY t.name, c.ordinal
                """, systemId)) {
            String tid = String.valueOf(r.get("table_id"));
            if (!picked.isEmpty() && !picked.contains(tid)) {
                continue;
            }
            Map<String, Object> table = tables.get(tid);
            if (table == null) {
                table = Dyn.of("id", tid, "name", r.get("table_name"), "schema", r.get("db_schema"),
                        "comment", r.get("table_comment"),
                        "need_table", needs((String) r.get("table_comment"), (String) r.get("table_ai"), overwrite),
                        "columns", new ArrayList<Map<String, Object>>());
                tables.put(tid, table);
            }
            String comment = ContractService.firstText(r.get("note"), r.get("comment"));  // 사람이 적은 설명이 있으면 채우지 않는다
            Dyn.items(table.get("columns")).add(Dyn.of("name", r.get("column_name"), "type", r.get("data_type"),
                    "comment", comment, "is_pk", Boolean.TRUE.equals(r.get("is_pk")),
                    "need", needs(comment, (String) r.get("ai_comment"), overwrite)));
        }
        for (String tid : picked) {
            if (!tables.containsKey(tid)) {
                throw new ApiException(404, "테이블을 찾을 수 없습니다: " + tid);
            }
        }
        List<Map<String, Object>> out = new ArrayList<>();
        for (Map<String, Object> table : tables.values()) {
            if (Boolean.TRUE.equals(table.get("need_table")) || !neededColumns(table).isEmpty()) {
                out.add(table);
            }
        }
        return out;
    }

    /** 테이블 하나의 빈 설명을 추론해 저장한다 → [테이블 설명을 채웠나(0|1), 채운 컬럼 수] */
    int[] describeTable(Map<String, Object> row, Dialect d, Map<String, Object> table, Llm model) {
        Object[] sampled = samples(row, d, table);
        @SuppressWarnings("unchecked")
        List<List<Object>> samples = (List<List<Object>>) sampled[0];
        String note = (String) sampled[1];
        List<String> asked = neededColumns(table);
        List<Llm.Message> messages = new ArrayList<>(List.of(Llm.Message.system(SYSTEM_PROMPT),
                Llm.Message.user(buildPrompt(row, table, asked, samples, note))));
        Llm.Reply reply = model.invoke(messages);
        Map<String, Object> answer;
        try {
            answer = LlmCalls.parseJson(reply.content());
        } catch (IllegalArgumentException error) {
            messages.add(Llm.Message.assistant(reply.content()));
            messages.add(Llm.Message.user("형식이 틀렸습니다: " + error.getMessage() + ". 설명 없이 JSON 객체 하나만 다시 답하십시오."));
            answer = LlmCalls.parseJson(model.invoke(messages).content());
        }
        String tableDesc = Boolean.TRUE.equals(table.get("need_table")) ? clean(answer.get("table")) : "";
        Map<String, String> columnDescs = readColumns(answer, asked);
        store((String) table.get("id"), tableDesc, columnDescs);
        log.info("{} 설명: 테이블 {}, 컬럼 {}/{}", table.get("name"), tableDesc.isEmpty() ? "-" : "채움", columnDescs.size(), asked.size());
        return new int[] {tableDesc.isEmpty() ? 0 : 1, columnDescs.size()};
    }

    static String buildPrompt(Map<String, Object> row, Map<String, Object> table, List<String> asked, List<List<Object>> samples,
            String note) {
        List<String> lines = new ArrayList<>();
        String domain = Py.squeeze(row.get("domain_desc"));
        if (!domain.isEmpty()) {
            lines.add("업무 영역: " + domain);
        }
        lines.add("테이블: " + table.get("name"));
        if (Py.truthy(table.get("comment"))) {
            lines.add("테이블 설명: " + table.get("comment"));
        }
        lines.add("테이블 설명 필요: " + (Boolean.TRUE.equals(table.get("need_table")) ? "예" : "아니오"));
        lines.add("");
        lines.add("컬럼 (이름 | 타입 | 설명):");
        List<String> names = new ArrayList<>();
        for (Map<String, Object> c : Dyn.maps(table.get("columns"))) {
            String desc = Boolean.TRUE.equals(c.get("need")) ? "(설명 필요)"
                    : (Py.truthy(c.get("comment")) ? (String) c.get("comment") : "(설명 필요 없음)");
            String pk = Boolean.TRUE.equals(c.get("is_pk")) ? " PK" : "";
            lines.add("- " + c.get("name") + " | " + c.get("type") + pk + " | " + desc);
            names.add((String) c.get("name"));
        }
        lines.add("");
        if (!samples.isEmpty()) {
            lines.add("샘플 " + samples.size() + "행 (컬럼 순서: " + String.join(", ", names) + "):");
            for (List<Object> values : samples) {
                lines.add(Json.dumps(values));
            }
        } else {
            lines.add("샘플 값 없음 (" + note + ")");
        }
        lines.add("");
        lines.add("설명이 필요한 컬럼: " + (asked.isEmpty() ? "없음" : String.join(", ", asked)));
        return String.join("\n", lines);
    }

    /** 모델 답 → {컬럼: 설명}. 묻지 않은 것과 빈 설명은 버린다. */
    static Map<String, String> readColumns(Map<String, Object> answer, List<String> asked) {
        Map<String, String> wanted = new LinkedHashMap<>();
        for (String name : asked) {
            wanted.put(name.toLowerCase(), name);
        }
        Map<String, String> columns = new LinkedHashMap<>();
        Map<String, Object> given = Dyn.map(answer.get("columns"));
        if (given != null) {
            for (Map.Entry<String, Object> e : given.entrySet()) {
                String name = wanted.get(e.getKey().toLowerCase());
                String text = clean(e.getValue());
                if (name != null && !text.isEmpty()) {
                    columns.put(name, text);
                }
            }
        }
        return columns;
    }

    /** 앞에서 몇 행. 읽지 못해도 설명은 이름 · 타입으로 추론한다 — 그때 이유를 돌려준다. → [행 목록, 이유] */
    private Object[] samples(Map<String, Object> row, Dialect d, Map<String, Object> table) {
        List<String> quoted = new ArrayList<>();
        for (Map<String, Object> c : Dyn.maps(table.get("columns"))) {
            quoted.add(d.q((String) c.get("name")));
        }
        String sql = "SELECT " + String.join(", ", quoted) + " FROM " + d.table((String) table.get("name"),
                Py.text(table.get("schema"))) + " " + d.limit(SAMPLE_ROWS);
        List<List<Object>> fetched;
        try (TargetSession cur = targetDb.readOnly(row, SAMPLE_TIMEOUT_S)) {
            fetched = cur.rows(sql);
        } catch (SQLException | RuntimeException error) {
            return new Object[] {new ArrayList<>(), "샘플을 읽지 못했습니다: " + Py.firstLine(error)};
        }
        List<List<Object>> out = new ArrayList<>();
        for (List<Object> r : fetched) {
            List<Object> values = new ArrayList<>();
            for (Object v : r) {
                values.add(sampleValue(v));
            }
            out.add(values);
        }
        return new Object[] {out, out.isEmpty() ? "빈 테이블" : ""};
    }

    static Object sampleValue(Object value) {
        if (value == null || value instanceof Boolean || value instanceof Integer || value instanceof Long
                || value instanceof Short || value instanceof Double || value instanceof Float) {
            return value;
        }
        if (value instanceof BigDecimal big) {
            return big.doubleValue();
        }
        if (value instanceof byte[]) {
            return "<binary>";
        }
        if (value instanceof LocalDateTime t) {
            return Times.iso(t);
        }
        if (value instanceof OffsetDateTime t) {
            return Times.iso(t);
        }
        if (value instanceof LocalDate t) {
            return Times.day(t);
        }
        if (value instanceof LocalTime t) {
            return Times.time(t);
        }
        String text = String.valueOf(value);
        return text.length() <= VALUE_CHARS ? text : text.substring(0, VALUE_CHARS - 3) + "...";
    }

    private void store(String tableId, String tableDesc, Map<String, String> columnDescs) {
        db.transaction(tx -> {
            if (!tableDesc.isEmpty()) {
                tx.update("UPDATE meta_table SET ai_comment = ?, updated_at = now() WHERE id = ?", tableDesc, tableId);
            }
            List<Object[]> rows = new ArrayList<>();
            for (Map.Entry<String, String> e : columnDescs.entrySet()) {
                rows.add(new Object[] {e.getValue(), tableId, e.getKey()});
            }
            tx.batch("UPDATE meta_column SET ai_comment = ?, updated_at = now() WHERE table_id = ? AND name = ?", rows);
        });
    }

    static boolean needs(String comment, String aiComment, boolean overwrite) {
        if (comment != null && !comment.strip().isEmpty()) {
            return false;
        }
        return overwrite || aiComment == null || aiComment.strip().isEmpty();
    }

    static List<String> neededColumns(Map<String, Object> table) {
        List<String> out = new ArrayList<>();
        for (Map<String, Object> c : Dyn.maps(table.get("columns"))) {
            if (Boolean.TRUE.equals(c.get("need"))) {
                out.add((String) c.get("name"));
            }
        }
        return out;
    }

    static String clean(Object value) {
        String text = Py.squeeze(Py.text(value));
        return text.length() <= DESC_CHARS ? text : text.substring(0, DESC_CHARS);
    }

    // ── 관계 ──────────────────────────────────────────────────────────────

    /** auto 관계를 새로 추론해 바꿔 넣는다 → 넣은 수 */
    public int inferAndStoreRelations(String systemId) {
        Map<String, Map<String, Object>> tables = contracts.loadTables(systemId);
        Set<String> existing = new HashSet<>();
        for (Map<String, Object> r : db.query("""
                SELECT lt.name AS left_table, r.left_column, rt.name AS right_table, r.right_column
                  FROM meta_relation r
                  JOIN meta_table lt ON lt.id = r.left_table_id
                  JOIN meta_table rt ON rt.id = r.right_table_id
                 WHERE r.system_id = ? AND r.source <> 'auto'
                """, systemId)) {
            existing.add(pairKey((String) r.get("left_table"), (String) r.get("left_column"), (String) r.get("right_table"),
                    (String) r.get("right_column")));
        }
        List<Object[]> rows = new ArrayList<>();
        for (Map<String, Object> r : inferRelations(new ArrayList<>(tables.values()))) {
            String lt = (String) r.get("left_table");
            String lc = (String) r.get("left_column");
            String rt = (String) r.get("right_table");
            String rc = (String) r.get("right_column");
            if (existing.contains(pairKey(lt, lc, rt, rc))) {
                continue;
            }
            rows.add(new Object[] {UUID.randomUUID().toString(), systemId, "auto:" + lt + "." + lc + "->" + rt + "." + rc,
                    tables.get(lt).get("id"), lc, tables.get(rt).get("id"), rc});
        }
        db.transaction(tx -> {
            tx.update("DELETE FROM meta_relation WHERE system_id = ? AND source = 'auto'", systemId);
            tx.batch("""
                    INSERT INTO meta_relation (id, system_id, constraint_name, left_table_id, left_column,
                                               right_table_id, right_column, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'auto')
                    """, rows);
        });
        log.info("system {}: 관계 {}개 추론", systemId, rows.size());
        return rows.size();
    }

    /**
     * 컬럼 이름 · 타입 · PK 로 관계를 추론한다 → [{left_table, left_column, right_table, right_column}]
     *
     * PK 가 컬럼 하나인 테이블만 가리킬 수 있다.
     * - PK 이름이 고유하면 (vehicle_id): 다른 테이블의 같은 이름 컬럼이 그 PK 를 가리킨다.
     *   같은 PK 이름인 테이블이 여럿이면 이름이 맞는 테이블(vehicle, tb_vehicle)만 남긴다.
     * - PK 이름이 흔하면 (id): '<테이블>_id' 처럼 적힌 컬럼만 그 테이블을 가리킨다.
     * 타입 갈래(숫자 · 문자 · 시각)가 다르면 잇지 않는다.
     */
    public static List<Map<String, Object>> inferRelations(List<Map<String, Object>> tables) {
        Map<String, Map<String, Object>> pkOf = new LinkedHashMap<>();
        for (Map<String, Object> t : tables) {
            List<Map<String, Object>> keys = new ArrayList<>();
            for (Map<String, Object> c : Dyn.maps(t.get("columns"))) {
                if (Py.truthy(c.get("is_pk"))) {
                    keys.add(c);
                }
            }
            if (keys.size() == 1) {
                pkOf.put((String) t.get("name"), keys.get(0));
            }
        }

        List<Map<String, Object>> out = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (Map<String, Object> t : tables) {
            String tableName = (String) t.get("name");
            Map<String, Object> ownPk = pkOf.get(tableName);
            for (Map<String, Object> c : Dyn.maps(t.get("columns"))) {
                String column = ((String) c.get("name")).toLowerCase();
                // 자기 PK 이고 테이블 이름도 맞으면 이 테이블이 그 키의 본 테이블이다 (vehicle.vehicle_id) — 가리키지 않는다
                if (ownPk == c && namesMatch(tableName, stem(column))) {
                    continue;
                }
                String target = target(column, tableName, pkOf);
                if (target == null) {
                    continue;
                }
                // PK 끼리는 이름이 맞는 본 테이블일 때만 잇는다 (vehicle_ext.vehicle_id → vehicle)
                if (ownPk == c && !namesMatch(target, stem(column))) {
                    continue;
                }
                Map<String, Object> pk = pkOf.get(target);
                if (!Compiler.typeFamily(Dyn.text(c, "type")).equals(Compiler.typeFamily(Dyn.text(pk, "type")))) {
                    continue;
                }
                String key = pairKey(tableName, (String) c.get("name"), target, (String) pk.get("name"));
                if (!seen.add(key)) {
                    continue;
                }
                out.add(Dyn.of("left_table", tableName, "left_column", c.get("name"), "right_table", target,
                        "right_column", pk.get("name")));
            }
        }
        return out;
    }

    /** 컬럼이 가리키는 테이블. 없거나 여럿이면 null */
    static String target(String column, String own, Map<String, Map<String, Object>> pkOf) {
        String stem = stem(column);
        List<String> candidates = new ArrayList<>();
        for (Map.Entry<String, Map<String, Object>> e : pkOf.entrySet()) {
            String name = e.getKey();
            if (name.equals(own)) {
                continue;
            }
            String key = ((String) e.getValue().get("name")).toLowerCase();
            if (GENERIC_KEYS.contains(key)) {
                // vehicle_id → vehicle.id : 컬럼이 '<테이블>_<PK>' 꼴일 때만
                if (column.endsWith("_" + key) && namesMatch(name, column.substring(0, column.length() - key.length() - 1))) {
                    candidates.add(name);
                }
            } else if (key.equals(column)) {
                // 키 꼴(_id, _no …)이 아닌 PK (name, company)는 흔한 단어라 테이블 이름이 맞을 때만
                if (!stem(key).isEmpty() || namesMatch(name, key)) {
                    candidates.add(name);
                }
            }
        }
        if (candidates.size() > 1 && !stem.isEmpty()) {
            List<String> named = new ArrayList<>();
            for (String name : candidates) {
                if (namesMatch(name, stem)) {
                    named.add(name);
                }
            }
            candidates = named;
        }
        return candidates.size() == 1 ? candidates.get(0) : null;
    }

    /** 테이블 이름(스키마를 뗀)이 stem 이거나 접두어 + stem 인가 (vehicle, tb_vehicle, t_vehicle) */
    static boolean namesMatch(String table, String stem) {
        String[] parts = table.split("\\.");
        String bare = parts[parts.length - 1].toLowerCase();
        return !stem.isEmpty() && (bare.equals(stem) || bare.endsWith("_" + stem) || bare.equals(stem + "s"));
    }

    static String stem(String column) {
        for (String suffix : KEY_SUFFIXES) {
            if (column.endsWith(suffix) && column.length() > suffix.length()) {
                return column.substring(0, column.length() - suffix.length());
            }
        }
        return "";
    }

    /** 방향 없이 같은 관계인지 본다 */
    static String pairKey(String leftTable, String leftColumn, String rightTable, String rightColumn) {
        String a = leftTable + "\u0000" + leftColumn;
        String b = rightTable + "\u0000" + rightColumn;
        return a.compareTo(b) <= 0 ? a + "\u0001" + b : b + "\u0001" + a;
    }

    // ── 기록 ──────────────────────────────────────────────────────────────

    private void close(String runId, String status, String error) {
        db.update("UPDATE meta_describe_run SET status = ?, error = ?, finished_at = now() WHERE id = ?", status, error, runId);
    }

    private DescribeRun toRun(Map<String, Object> r) {
        return new DescribeRun(
                String.valueOf(r.get("id")),
                String.valueOf(r.get("system_id")),
                RunRecords.status(r, background),
                r.get("options") == null ? new LinkedHashMap<>() : Dyn.map(r.get("options")),
                RunRecords.integer(r, "tables_total"),
                RunRecords.integer(r, "tables_done"),
                RunRecords.integer(r, "tables_filled"),
                RunRecords.integer(r, "columns_filled"),
                r.get("relations_added") == null ? null : ((Number) r.get("relations_added")).intValue(),
                Dyn.items(r.get("failures")),
                (String) r.get("error"),
                RunRecords.started(r),
                RunRecords.finished(r));
    }
}
