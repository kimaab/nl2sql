package com.nl2sql.service;

import java.math.BigDecimal;
import java.sql.SQLException;
import java.time.LocalDateTime;
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
import com.nl2sql.llm.Step;
import com.nl2sql.model.RunModels.ValidationCheck;
import com.nl2sql.model.RunModels.ValidationItem;
import com.nl2sql.model.RunModels.ValidationRequest;
import com.nl2sql.model.RunModels.ValidationRun;
import com.nl2sql.model.RunModels.ValidationRunDetail;
import com.nl2sql.sqlast.SqlAst;

/**
 * 등록된 지표 검증: 지표마다 여러 점검을 돌려 통과 · 주의 · 실패를 매기고, 점검마다 이유와 근거를 남긴다 (metric_validate.py).
 *
 * 점검 (앞 단계가 실패하면 뒤 단계는 건너뛴다)
 *   compile    저장된 AST 가 지금 스키마로 컴파일되는가
 *   roundtrip  원본 SQL 을 지금 다시 옮겨도 저장된 AST 와 같은가 (등록 뒤 UTC 보정 · 컬럼이 바뀌었는지)
 *   duplicate  다른 지표와 컴파일 결과가 같은가
 *   plan       실행 계획이 큰 테이블 전체를 훑는가 (EXPLAIN 만)
 *   verify     원본 SQL 과 AST 로 만든 SQL 의 결과가 대상 DB 에서 같은가 (등록 확인과 같은 비교)
 *   result     결과가 비었거나 값이 모두 NULL 인 컬럼이 있는가 (표본 몇 행을 남긴다)
 *   llm        LLM 이 SQL 이 이름 · 설명과 맞는지, JOIN 으로 부풀지 않는지 등을 검토한다
 */
@Service
public class MetricValidateService {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.validate");

    static final int SAMPLE_ROWS = 20;
    static final int RESULT_TIMEOUT_S = 60;
    static final Map<String, String> LABELS = Map.of("compile", "컴파일", "roundtrip", "원본 재변환", "duplicate", "중복",
            "plan", "실행 계획", "verify", "결과 비교", "result", "결과 점검", "llm", "의미 검토");

    static final String REVIEW_SYSTEM = """
            당신은 '{name}' 데이터베이스({driver})에 등록된 업무 지표를 검토하는 역할입니다.
            지표는 이름 · 설명 · 같은 말과 SQL 로 되어 있고, 질문마다 다른 LLM 이 설명을 읽고 지표를 고른 뒤 SQL 의 조건만 바꿔 씁니다.
            {domain}
            [점검할 것]
            - SQL 이 이름과 설명이 말하는 계산을 하는가 (쓰는 컬럼, 집계 방식, 묶음 단위, 빼는 행).
            - JOIN 으로 행이 불어나 합계가 부풀지 않는가 (팩트 테이블끼리 JOIN, 1:N 의 N 쪽을 JOIN 한 뒤 SUM).
            - 나눗셈에 0 나누기나 정수 나눗셈이 없는가.
            - [계산 필요] 컬럼을 그대로 SUM · AVG 하지 않는가.
            - NULL 때문에 결과가 틀리지 않는가 (COUNT(컬럼) 과 COUNT(*) 의 차이, NULL 이 섞인 합과 비율).
            - 설명만 읽고 이 지표를 고를 수 있는가 (설명이 비었거나 계산과 다르면 문제).
            - [결과 표본] 의 값이 상식에 맞는가 (음수가 나올 수 없는 값의 음수, 0 ~ 1 을 벗어난 비율 등).
            [규칙]
            - 주어진 정보로 확인되는 문제만 적습니다. 추측은 적지 않습니다.
            - severity 는 fail(결과가 틀림) 또는 warn(틀릴 수 있거나 설명이 부족함) 입니다.
            - verdict 는 문제가 없으면 pass, warn 만 있으면 warn, fail 이 하나라도 있으면 fail 입니다.
            - summary 에는 판단을 한두 문장으로, issue 의 message 에는 무엇이 왜 문제인지와 고칠 방법을 적습니다.
            - 설명 없이 JSON 객체 하나로만 답합니다:
              {"verdict": "pass", "summary": "판단 요약", "issues": [{"severity": "warn", "message": "문제와 고칠 방법"}]}""";

    private static final String DONE = "(SELECT count(*) FROM metric_validation_item i WHERE i.run_id = r.id) AS done";

    private final MetaDb db;
    private final Background background;
    private final ContractService contracts;
    private final Connections connections;
    private final MetricService metrics;
    private final MetricAutoService auto;
    private final TargetDb targetDb;

    public MetricValidateService(MetaDb db, Background background, ContractService contracts, Connections connections,
            MetricService metrics, MetricAutoService auto, TargetDb targetDb) {
        this.db = db;
        this.background = background;
        this.contracts = contracts;
        this.connections = connections;
        this.metrics = metrics;
        this.auto = auto;
        this.targetDb = targetDb;
    }

    // ── 시작 · 조회 ───────────────────────────────────────────────────────

    /** 고른 지표만 검증한다. 고르지 않으면 돌리지 않는다 — 전체도 사람이 모두 골라서 돌린다. */
    public ValidationRun startValidation(String systemId, ValidationRequest req, Llm model) {
        if (req.metricIds().isEmpty()) {
            throw new ApiException(400, "검증할 지표를 고르십시오");
        }
        List<Map<String, Object>> rows = pick(systemId, req.metricIds());
        List<String> ids = new ArrayList<>();
        for (Map<String, Object> r : rows) {
            ids.add(String.valueOf(r.get("id")));
        }
        String runId = UUID.randomUUID().toString();
        Map<String, Object> options = Dyn.of("run_sql", req.runSql(), "use_llm", req.useLlm(), "allow_full_scan",
                req.allowFullScan(), "metric_ids", ids);
        db.update("INSERT INTO metric_validation_run (id, system_id, options, total) VALUES (?, ?, ?, ?)",
                runId, systemId, Json.dumps(options), ids.size());

        boolean started = background.start("validate:" + systemId, runId, () -> runValidation(systemId, runId, ids, req, model),
                error -> close(runId, "error", Py.firstLine(error)));
        if (!started) {
            db.update("DELETE FROM metric_validation_run WHERE id = ?", runId);
            throw new ApiException(409, "이 시스템에서 지표 검증이 이미 돌고 있습니다");
        }
        log.info("지표 검증 시작: run={} metrics={}", runId, ids.size());
        ValidationRunDetail detail = getValidation(systemId, runId);
        return new ValidationRun(detail.id(), detail.systemId(), detail.status(), detail.options(), detail.total(), detail.done(),
                detail.passed(), detail.warned(), detail.failed(), detail.promptTokens(), detail.completionTokens(),
                detail.error(), detail.startedAt(), detail.finishedAt());
    }

    public List<ValidationRun> listValidations(String systemId, int limit) {
        List<ValidationRun> runs = new ArrayList<>();
        for (Map<String, Object> r : db.query("SELECT r.*, " + DONE
                + " FROM metric_validation_run r WHERE r.system_id = ? ORDER BY r.started_at DESC LIMIT ?", systemId, limit)) {
            runs.add(toRun(r));
        }
        return runs;
    }

    public ValidationRunDetail getValidation(String systemId, String runId) {
        Map<String, Object> row = db.one("SELECT r.*, " + DONE + " FROM metric_validation_run r WHERE r.id = ? AND r.system_id = ?",
                runId, systemId);
        if (row == null) {
            throw new ApiException(404, "지표 검증 실행을 찾을 수 없습니다");
        }
        List<ValidationItem> items = new ArrayList<>();
        for (Map<String, Object> r : db.query("SELECT * FROM metric_validation_item WHERE run_id = ? ORDER BY id", runId)) {
            items.add(toItem(r));
        }
        return ValidationRunDetail.of(toRun(row), items);
    }

    /** 지표 하나의 검증 이력 (최근 것이 먼저) */
    public List<ValidationItem> metricValidations(String systemId, String metricId, int limit) {
        List<ValidationItem> items = new ArrayList<>();
        for (Map<String, Object> r : db.query("""
                SELECT i.* FROM metric_validation_item i
                  JOIN metric_validation_run r ON r.id = i.run_id
                 WHERE r.system_id = ? AND i.metric_id = ?
                 ORDER BY i.created_at DESC LIMIT ?
                """, systemId, metricId, limit)) {
            items.add(toItem(r));
        }
        return items;
    }

    private List<Map<String, Object>> pick(String systemId, List<String> metricIds) {
        Set<String> wanted = new HashSet<>();
        for (String i : metricIds) {
            wanted.add(SystemService.uuid(i));
        }
        List<Map<String, Object>> picked = new ArrayList<>();
        for (Map<String, Object> r : contracts.loadMetricRows(systemId)) {
            if (wanted.contains(String.valueOf(r.get("id")))) {
                picked.add(r);
            }
        }
        if (picked.size() != wanted.size()) {
            throw new ApiException(404, "이 시스템에 없는 지표가 있습니다");
        }
        return picked;
    }

    // ── 실행 ──────────────────────────────────────────────────────────────

    void runValidation(String systemId, String runId, List<String> ids, ValidationRequest req, Llm model) {
        Map<String, Object> contract = contracts.loadContract(systemId);
        Map<String, Object> row = req.runSql() ? connections.row(systemId) : null;
        Compiler compiler = new Compiler(contract);
        // 중복 점검은 고른 지표가 아니라 시스템의 모든 지표와 견준다
        List<Map<String, Object>> everything = contracts.loadMetricRows(systemId);
        Map<String, String[]> compiledAll = new LinkedHashMap<>();
        for (Map<String, Object> m : everything) {
            try {
                compiledAll.put(String.valueOf(m.get("id")), new String[] {(String) m.get("name"), compiler.compile(m.get("ast"))});
            } catch (IllegalArgumentException ignored) {
                // 컴파일되지 않는 지표는 중복 비교에서 뺀다
            }
        }
        Set<String> wanted = new HashSet<>(ids);
        int promptTokens = 0;
        int completionTokens = 0;
        for (Map<String, Object> m : everything) {
            if (!wanted.contains(String.valueOf(m.get("id")))) {
                continue;
            }
            long started = System.nanoTime();
            Step step = new Step("지표 검증", model != null ? model.modelName() : "");
            List<Map<String, Object>> checks;
            try {
                checks = validateMetric(systemId, m, contract, compiler, compiledAll, row, req.useLlm() ? model : null, step,
                        req.allowFullScan());
            } catch (RuntimeException error) {
                log.error("지표 검증 실패: {}", m.get("name"), error);
                checks = new ArrayList<>(List.of(check("compile", "fail", "검증 중 오류가 났습니다: " + Py.firstLine(error), null)));
            }
            promptTokens += step.promptTokens;
            completionTokens += step.completionTokens;
            String[] verdict = overall(checks);
            db.update("""
                    INSERT INTO metric_validation_item (run_id, metric_id, metric_name, metric_version, verdict, summary, checks, elapsed_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, runId, String.valueOf(m.get("id")), m.get("name"), m.get("version"), verdict[0], verdict[1],
                    Json.dumps(checks), (int) ((System.nanoTime() - started) / 1_000_000));
            progress(runId, promptTokens, completionTokens);
        }
        close(runId, "done", null);
        log.info("지표 검증 끝: run={}", runId);
    }

    /**
     * 지표 하나의 점검 목록. row 가 없으면 대상 DB 점검을, model 이 없으면 의미 검토를 건너뛴다.
     * 실행 계획이 큰 테이블 전체를 훑으면 allowFullScan 일 때만 결과 비교 · 결과 점검을 돌린다.
     */
    List<Map<String, Object>> validateMetric(String systemId, Map<String, Object> m, Map<String, Object> contract,
            Compiler compiler, Map<String, String[]> compiledAll, Map<String, Object> row, Llm model, Step step,
            boolean allowFullScan) {
        List<Map<String, Object>> checks = new ArrayList<>();

        // 1) 컴파일
        String compiled;
        try {
            compiled = compiler.compile(m.get("ast"));
        } catch (IllegalArgumentException error) {
            compiled = null;
            checks.add(check("compile", "fail", "지금 스키마로 컴파일되지 않습니다 — " + error.getMessage(), null));
        }
        if (compiled != null) {
            if ("broken".equals(m.get("status"))) {
                checks.add(check("compile", "warn", "컴파일은 되지만 상태가 '깨짐' 으로 남아 있습니다 — 동기화하면 다시 매겨집니다",
                        Dyn.of("sql", compiled, "broken_reason", m.get("broken_reason"))));
            } else {
                checks.add(check("compile", "pass", "지금 스키마로 컴파일됩니다", Dyn.of("sql", compiled)));
            }
        }

        // 2) 원본 재변환
        try {
            SqlAst.Parsed parsed = SqlAst.parseSql((String) m.get("source_sql"), contract);
            if (Json.same(parsed.ast(), m.get("ast"))) {
                checks.add(check("roundtrip", "pass", "원본 SQL 을 다시 옮겨도 저장된 AST 와 같습니다",
                        Dyn.of("warnings", parsed.warnings())));
            } else {
                checks.add(check("roundtrip", "warn",
                        "원본 SQL 을 지금 다시 옮기면 저장된 AST 와 다릅니다 — 등록 뒤 UTC 보정이나 컬럼이 바뀌었을 수 있습니다. "
                                + "지표를 다시 저장하면 맞춰집니다",
                        Dyn.of("stored_ast", m.get("ast"), "current_ast", parsed.ast(), "warnings", parsed.warnings())));
            }
        } catch (IllegalArgumentException error) {
            checks.add(check("roundtrip", "warn", "원본 SQL 을 지금 스키마로 다시 옮기지 못합니다 — " + error.getMessage(), null));
        }

        // 3) 중복
        if (compiled == null) {
            checks.add(skip("duplicate", "컴파일되지 않아 건너뛰었습니다"));
        } else {
            List<String> same = new ArrayList<>();
            for (Map.Entry<String, String[]> e : compiledAll.entrySet()) {
                if (!e.getKey().equals(String.valueOf(m.get("id"))) && e.getValue()[1].equals(compiled)) {
                    same.add(e.getValue()[0]);
                }
            }
            if (!same.isEmpty()) {
                checks.add(check("duplicate", "warn", "컴파일 결과가 같은 지표가 있습니다: " + String.join(", ", same),
                        Dyn.of("metrics", same)));
            } else {
                checks.add(check("duplicate", "pass", "같은 계산의 다른 지표가 없습니다", null));
            }
        }

        // 4) ~ 6) 대상 DB
        Map<String, Object> sample = null;
        if (row == null) {
            for (String key : List.of("plan", "verify", "result")) {
                checks.add(skip(key, "대상 DB 점검을 끄고 돌렸습니다"));
            }
        } else if (compiled == null) {
            for (String key : List.of("plan", "verify", "result")) {
                checks.add(skip(key, "컴파일되지 않아 건너뛰었습니다"));
            }
        } else {
            Map<String, Object> plan = planCheck(row, (String) m.get("source_sql"), compiled);
            checks.add(plan);
            if (Py.truthy(Dyn.map(plan.get("detail")).get("full_scans")) && !allowFullScan) {
                for (String key : List.of("verify", "result")) {
                    checks.add(skip(key, "실행 계획이 큰 테이블 전체를 훑어 실행하지 않았습니다 (테스트 DB 면 '전체 훑기 허용' 으로 다시 돌리십시오)"));
                }
            } else {
                checks.add(verifyCheck(systemId, (String) m.get("source_sql"), compiled));
                Map<String, Object> result = resultCheck(row, compiled);
                checks.add(result);
                if (Py.truthy(Dyn.map(result.get("detail")).get("rows"))) {
                    sample = Dyn.map(result.get("detail"));
                }
            }
        }

        // 7) 의미 검토
        if (model == null) {
            checks.add(skip("llm", "의미 검토를 끄고 돌렸습니다"));
        } else {
            checks.add(review(model, contract, m, sample, step));
        }
        return checks;
    }

    private Map<String, Object> planCheck(Map<String, Object> row, String original, String compiled) {
        if ("oracle".equals(row.get("driver"))) {
            return skip("plan", "Oracle 은 읽기 전용 트랜잭션에서 실행 계획을 볼 수 없어 건너뛰었습니다");
        }
        List<String> scanned = auto.planFullScans(row, original, compiled);
        if (!scanned.isEmpty()) {
            return check("plan", "warn", "실행 계획이 " + String.join(", ", scanned)
                    + " 전체를 훑습니다 — 질문마다 기간 조건이 붙지 않으면 운영 DB 에 부담이 됩니다", Dyn.of("full_scans", scanned));
        }
        return check("plan", "pass", "큰 테이블 전체를 훑지 않습니다", null);
    }

    private Map<String, Object> verifyCheck(String systemId, String original, String compiled) {
        int rows;
        try {
            rows = metrics.verify(systemId, original, compiled);
        } catch (IllegalArgumentException error) {
            return check("verify", "fail", error.getMessage(), null);
        } catch (ApiException error) {
            return check("verify", "fail", error.detail(), null);
        }
        return check("verify", "pass", "원본 SQL 과 AST 로 만든 SQL 의 결과가 같습니다 (" + Py.comma(rows) + "행)", Dyn.of("rows", rows));
    }

    /** 결과 표본 몇 행과 컬럼마다 NULL 수. 결과가 비었거나 값이 모두 NULL 인 컬럼이 있으면 주의. */
    private Map<String, Object> resultCheck(Map<String, Object> row, String compiled) {
        Dialect d = new Dialect((String) row.get("driver"));
        List<String> columns;
        List<List<Object>> fetched;
        List<Object> stats;
        try (TargetSession cur = targetDb.readOnly(row, RESULT_TIMEOUT_S)) {
            TargetSession.Result result = cur.query("SELECT * FROM (" + compiled + ") t " + d.limit(SAMPLE_ROWS), -1);
            columns = result.columns();
            fetched = result.rows();
            List<String> counts = new ArrayList<>();
            for (String c : columns) {
                counts.add("COUNT(" + d.q(c) + ")");
            }
            stats = cur.one("SELECT COUNT(*), " + String.join(", ", counts) + " FROM (" + compiled + ") t");
        } catch (ApiException error) {
            return check("result", "fail", error.detail(), null);
        } catch (SQLException | RuntimeException error) {
            return check("result", "fail", "결과를 읽지 못했습니다: " + Py.firstLine(error), null);
        }
        long total = ((Number) stats.get(0)).longValue();
        Map<String, Object> nulls = new LinkedHashMap<>();
        List<String> allNull = new ArrayList<>();
        for (int i = 0; i < columns.size(); i++) {
            long n = total - ((Number) stats.get(i + 1)).longValue();
            nulls.put(columns.get(i), n);
            if (total != 0 && n == total) {
                allNull.add(columns.get(i));
            }
        }
        List<List<String>> rows = new ArrayList<>();
        for (List<Object> r : fetched) {
            rows.add(shown(r));
        }
        Map<String, Object> detail = Dyn.of("total", total, "columns", columns, "rows", rows, "nulls", nulls);
        if (total == 0) {
            return check("result", "warn", "결과가 0행입니다 — 맞는 데이터가 없거나 JOIN · 조건이 행을 모두 걸러 냅니다", detail);
        }
        if (!allNull.isEmpty()) {
            return check("result", "warn", String.join(", ", allNull) + " 값이 모두 NULL 입니다 (" + Py.comma(total) + "행)", detail);
        }
        return check("result", "pass", Py.comma(total) + "행, 값이 모두 NULL 인 컬럼이 없습니다", detail);
    }

    private Map<String, Object> review(Llm model, Map<String, Object> contract, Map<String, Object> m, Map<String, Object> sample,
            Step step) {
        String driver = Py.truthy(contract.get("driver")) ? (String) contract.get("driver") : "postgresql";
        String domain = Py.truthy(contract.get("domain")) ? "[업무 영역]\n" + contract.get("domain") + "\n" : "";
        String system = REVIEW_SYSTEM.replace("{name}", Py.text(contract.get("name"))).replace("{driver}", driver)
                .replace("{domain}", domain);
        String user = reviewPrompt(contract, m, sample);
        Map<String, Object> parsed;
        try {
            parsed = LlmCalls.askJson(model, system, user, step, p -> {
                String verdict = Py.text(p.get("verdict")).strip().toLowerCase();
                if (!List.of("pass", "warn", "fail").contains(verdict)) {
                    throw new IllegalArgumentException("\"verdict\" 는 pass · warn · fail 중 하나입니다");
                }
                List<Object> issues = new ArrayList<>();
                for (Object raw : Dyn.items(p.get("issues"))) {
                    Map<String, Object> i = Dyn.map(raw);
                    if (i == null || Py.text(i.get("message")).strip().isEmpty()) {
                        continue;
                    }
                    String severity = "fail".equals(Py.text(i.get("severity")).toLowerCase()) ? "fail" : "warn";
                    issues.add(Dyn.of("severity", severity, "message", Py.squeeze(i.get("message"))));
                }
                return Dyn.of("verdict", verdict, "summary", Py.squeeze(p.get("summary")), "issues", issues);
            });
        } catch (IllegalArgumentException error) {
            return check("llm", "skip", error.getMessage(), null);
        } catch (RuntimeException error) {
            return check("llm", "skip", "LLM 을 부르지 못했습니다: " + Py.firstLine(error), null);
        }
        String summary = (String) parsed.get("summary");
        String message = !summary.isEmpty() ? summary : ("pass".equals(parsed.get("verdict")) ? "문제를 찾지 못했습니다" : "문제가 있습니다");
        return check("llm", (String) parsed.get("verdict"), message,
                Dyn.of("issues", parsed.get("issues"), "prompt_tokens", step.promptTokens, "completion_tokens", step.completionTokens));
    }

    static String reviewPrompt(Map<String, Object> contract, Map<String, Object> m, Map<String, Object> sample) {
        String driver = Py.truthy(contract.get("driver")) ? (String) contract.get("driver") : "postgresql";
        List<String> lines = new ArrayList<>();
        lines.add("[지표]\n이름: " + m.get("name"));
        String description = Py.squeeze(Py.text(m.get("description")));
        lines.add("설명: " + (description.isEmpty() ? "(없음)" : description));
        if (Py.truthy(m.get("synonyms"))) {
            lines.add("같은 말: " + String.join(", ", Dyn.strings(m.get("synonyms"))));
        }
        lines.add("SQL:\n" + m.get("source_sql"));

        List<String> used = Compiler.astTables(m.get("ast"));
        lines.add("\n[쓰는 테이블]");
        for (Map<String, Object> t : Dyn.maps(contract.get("tables"))) {
            if (used.contains((String) t.get("name"))) {
                String note = Py.squeeze(ContractService.firstText(t.get("purpose"), t.get("description")));
                lines.add(("[" + t.get("name") + "] " + note).stripTrailing());
                for (Map<String, Object> c : Dyn.maps(t.get("columns"))) {
                    lines.add(MetricAutoService.columnLine(c, driver));
                }
            }
        }
        List<String> pairs = new ArrayList<>();
        for (Map<String, Object> r : Dyn.maps(contract.get("relations"))) {
            if (used.contains((String) r.get("left_table")) && used.contains((String) r.get("right_table"))) {
                pairs.add("  - " + r.get("left_table") + "." + r.get("left_column") + " = " + r.get("right_table") + "."
                        + r.get("right_column"));
            }
        }
        if (!pairs.isEmpty()) {
            lines.add("\n[관계]");
            lines.addAll(pairs);
        }

        if (sample != null) {
            List<Object> rows = Dyn.items(sample.get("rows"));
            lines.add("\n[결과 표본] 전체 " + Py.comma(((Number) sample.get("total")).longValue()) + "행 중 " + rows.size() + "행");
            lines.add(String.join(" | ", Dyn.strings(sample.get("columns"))));
            for (Object r : rows) {
                lines.add(String.join(" | ", Dyn.strings(r)));
            }
        }
        return String.join("\n", lines);
    }

    /** → [판정, 요약]. 실패가 하나라도 있으면 fail, 주의만 있으면 warn. */
    static String[] overall(List<Map<String, Object>> checks) {
        String verdict = "pass";
        List<String> notes = new ArrayList<>();
        for (Map<String, Object> c : checks) {
            if ("fail".equals(c.get("status"))) {
                verdict = "fail";
            } else if ("warn".equals(c.get("status")) && verdict.equals("pass")) {
                verdict = "warn";
            }
        }
        for (String status : List.of("fail", "warn")) {
            for (Map<String, Object> c : checks) {
                if (status.equals(c.get("status")) && notes.size() < 3) {
                    notes.add(c.get("label") + ": " + c.get("message"));
                }
            }
        }
        return new String[] {verdict, notes.isEmpty() ? "모든 점검을 통과했습니다" : String.join(" / ", notes)};
    }

    // ── 기록 ─────────────────────────────────────────────────────────────

    static Map<String, Object> check(String key, String status, String message, Map<String, Object> detail) {
        return Dyn.of("key", key, "label", LABELS.get(key), "status", status, "message", message, "detail",
                detail == null ? new LinkedHashMap<>() : detail);
    }

    static Map<String, Object> skip(String key, String message) {
        return check(key, "skip", message, null);
    }

    private void progress(String runId, int promptTokens, int completionTokens) {
        db.update("""
                UPDATE metric_validation_run r
                   SET prompt_tokens = ?, completion_tokens = ?,
                       passed = (SELECT count(*) FROM metric_validation_item i WHERE i.run_id = r.id AND i.verdict = 'pass'),
                       warned = (SELECT count(*) FROM metric_validation_item i WHERE i.run_id = r.id AND i.verdict = 'warn'),
                       failed = (SELECT count(*) FROM metric_validation_item i WHERE i.run_id = r.id AND i.verdict = 'fail')
                 WHERE r.id = ?
                """, promptTokens, completionTokens, runId);
    }

    private void close(String runId, String status, String error) {
        db.update("UPDATE metric_validation_run SET status = ?, error = ?, finished_at = now() WHERE id = ?", status, error, runId);
    }

    private ValidationRun toRun(Map<String, Object> r) {
        return new ValidationRun(
                String.valueOf(r.get("id")),
                String.valueOf(r.get("system_id")),
                RunRecords.status(r, background),
                r.get("options") == null ? new LinkedHashMap<>() : Dyn.map(r.get("options")),
                RunRecords.integer(r, "total"),
                RunRecords.integer(r, "done"),
                RunRecords.integer(r, "passed"),
                RunRecords.integer(r, "warned"),
                RunRecords.integer(r, "failed"),
                RunRecords.integer(r, "prompt_tokens"),
                RunRecords.integer(r, "completion_tokens"),
                (String) r.get("error"),
                RunRecords.started(r),
                RunRecords.finished(r));
    }

    private static ValidationItem toItem(Map<String, Object> r) {
        List<ValidationCheck> checks = new ArrayList<>();
        for (Map<String, Object> c : Dyn.maps(r.get("checks"))) {
            checks.add(new ValidationCheck((String) c.get("key"), (String) c.get("label"), (String) c.get("status"),
                    (String) c.get("message"), c.get("detail") == null ? new LinkedHashMap<>() : Dyn.map(c.get("detail"))));
        }
        return new ValidationItem(
                ((Number) r.get("id")).longValue(),
                String.valueOf(r.get("run_id")),
                String.valueOf(r.get("metric_id")),
                (String) r.get("metric_name"),
                RunRecords.integer(r, "metric_version"),
                (String) r.get("verdict"),
                (String) r.get("summary"),
                checks,
                RunRecords.integer(r, "elapsed_ms"),
                Times.iso((OffsetDateTime) r.get("created_at")));
    }

    static List<String> shown(List<Object> values) {
        List<String> out = new ArrayList<>();
        for (Object v : values) {
            if (v == null) {
                out.add("NULL");
            } else if (v instanceof LocalDateTime t) {
                out.add(Times.seconds(t));
            } else if (v instanceof OffsetDateTime t) {
                out.add(Times.seconds(t));
            } else if (v instanceof BigDecimal big) {
                BigDecimal normalized = big.stripTrailingZeros();
                out.add(big.signum() == 0 ? "0" : normalized.scale() <= 0 ? normalized.toPlainString() : big.toPlainString());
            } else {
                String text = v instanceof Number n ? Py.repr(n) : String.valueOf(v);
                out.add(text.length() <= 60 ? text : text.substring(0, 57) + "...");
            }
        }
        return out;
    }
}
