package com.nl2sql.service;

import java.sql.SQLException;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.function.BiFunction;
import java.util.function.Function;

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
import com.nl2sql.db.Catalog;
import com.nl2sql.db.MetaDb;
import com.nl2sql.db.TargetDb;
import com.nl2sql.db.TargetSession;
import com.nl2sql.llm.Llm;
import com.nl2sql.llm.LlmCalls;
import com.nl2sql.llm.Step;
import com.nl2sql.model.MetricModels.Metric;
import com.nl2sql.model.MetricModels.MetricCheck;
import com.nl2sql.model.MetricModels.MetricInput;
import com.nl2sql.model.RunModels.AutoItem;
import com.nl2sql.model.RunModels.AutoRun;
import com.nl2sql.model.RunModels.AutoRunDetail;
import com.nl2sql.model.RunModels.AutoRunRequest;
import com.nl2sql.model.RunModels.AutoTry;
import com.nl2sql.sqlast.SqlAst;

/**
 * 지표 자동 등록: 테이블마다 LLM 이 지표 후보를 쓰고, 등록 확인을 통과한 것만 지표로 저장한다 (metric_auto.py).
 *
 * 후보 하나는 지표 등록과 같은 확인(AST 변환 → 컴파일 → 실행 전 확인 → 결과 비교)을 거치고,
 * 실패하면 오류를 붙여 그 후보만 다시 쓰게 한다. 등록됐든 아니든 후보마다 시도와 이유를 남긴다 (metric_auto_item).
 * 저장은 지표 등록(MetricService.createMetric)을 그대로 부른다 — 사람이 등록한 지표와 같은 길이다.
 */
@Service
public class MetricAutoService {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.auto");

    static final int MAX_ATTEMPTS = 3;  // 후보 하나를 고쳐 쓰는 최대 횟수
    static final int PLAN_TIMEOUT_S = 10;
    static final int MAX_RELATED = 12;  // 프롬프트에 싣는 이어진 테이블 수
    static final int MAX_RELATED_COLUMNS = 40;
    static final int MAX_OTHER_METRICS = 100;

    static final String AUTO_SYSTEM = """
            당신은 '{name}' 데이터베이스({driver})에 등록할 업무 지표를 찾아 SQL 로 쓰는 역할입니다.
            지표는 계산이 필요한 숫자입니다. 등록된 지표는 질문마다 다른 LLM 이 설명을 읽고 고른 뒤, SQL 의 기간 · 조건 · 묶음만 바꿔 씁니다.
            {domain}
            [규칙]
            - [대상 테이블] 로 계산하는 지표를 최대 {count}개 고릅니다. 등록할 지표는 질문을 받은 LLM 이 테이블과 컬럼만 보고는 바로 만들기 어려운 계산입니다:
              1) [계산 필요] 컬럼을 계산하는 것 (예: 누적값의 하루 MAX - MIN)
              2) 업무 규칙이 들어간 조건이나 분류 (예: 특정 코드 값만 세기, CASE 로 구간 나누기)
              3) 분자와 분모가 정해진 비율, 여러 집계를 섞은 식 (예: 경고 차량 수 / 전체 차량 수)
              4) 여러 테이블의 값을 함께 써야 정의되는 계산 (예: 판매 수량 × 상품 원가, 반품 금액 / 판매 금액)
            - 한 컬럼을 그대로 COUNT · SUM · AVG · MIN · MAX 하고 컬럼이나 날짜로 묶기만 하는 단순 집계는 쓰지 않습니다 (질문 LLM 이 바로 만듭니다).
            - [관계] 가 있으면 후보 중 하나 이상은 이어진 테이블을 JOIN 해야 정의되는 지표로 고릅니다.
            - 위에 맞는 계산이 없으면 억지로 만들지 않습니다. 개수를 채우지 못해도 됩니다.
            - [이미 있는 지표] 와 같은 계산은 쓰지 않습니다. 이름도 겹치지 않게 합니다.
            - 지표 하나는 계산 하나입니다. 개수 · 정렬 · 특정 값 조건은 넣지 않습니다 (질문마다 붙습니다).
            {period_rule}
            - 묶음 단위를 하나 정해 GROUP BY 합니다 (예: 날짜별, 매장별). 묶음이 없으면 전체 한 행입니다.
            - 다른 테이블은 [관계] 의 컬럼으로만 JOIN 합니다. 같은 테이블을 두 번 쓰지 않습니다 (셀프 조인은 쓸 수 없습니다).
            - JOIN 으로 행이 불어나 합계가 부풀지 않게 합니다. 팩트 테이블끼리 바로 JOIN 하지 않습니다.
            - 비율은 SUM(a) * 1.0 / NULLIF(SUM(b), 0) 처럼 적어 0 나누기와 정수 나눗셈을 막습니다.
            - [계산 필요] 컬럼은 그대로 SUM · AVG 하지 않습니다.
            - [UTC +n시간] 컬럼은 UTC 로 저장된 시각입니다. 현지 날짜로 묶을 때는 '컬럼 + n시간' 으로 적습니다.
            - 쓸 수 있는 문법: SELECT 문 하나, INNER · LEFT JOIN (ON a.x = b.x), WHERE, GROUP BY, HAVING, ORDER BY, LIMIT,
              SUM · AVG · MIN · MAX · COUNT · COUNT(DISTINCT 컬럼), CASE, COALESCE, NULLIF, ROUND, ABS, 사칙연산, 날짜로 자르기(DATE · DATE_TRUNC), ROW_NUMBER.
              쓸 수 없는 것: UNION, 하위 쿼리 JOIN, IN (하위 쿼리), DISTINCT ON, OFFSET, 날짜가 아닌 형 변환(CAST), 윈도 범위(ROWS · RANGE).
            - GROUP BY · ORDER BY 에 순번(1, 2)을 쓰지 않습니다. SELECT 결과 컬럼마다 별칭(AS)을 붙입니다.
            - description 에는 무엇을 어떻게 계산하는지(쓰는 컬럼, 묶음 단위, 빼는 행) 적습니다. 질문을 받은 LLM 이 이 지표가 맞는지 판단할 때 읽습니다.
            - rationale 에는 이 지표를 고른 이유를 한 문장으로 적습니다.
            - 지표로 삼을 계산이 없으면 metrics 를 빈 목록으로 두고 skip 에 이유를 적습니다.
            - 설명 없이 JSON 객체 하나로만 답합니다:
              {"metrics": [{"name": "지표 이름", "description": "계산 설명", "synonyms": ["같은 말"], "sql": "SELECT ...", "rationale": "고른 이유"}], "skip": ""}""";

    static final String FIX_REQUEST = """
            지표 '{name}' 의 SQL 이 {stage}: {error}
            이 지표 하나만 고쳐 설명 없이 JSON 객체 하나로 다시 답하십시오:
            {"name": "지표 이름", "description": "계산 설명", "synonyms": ["같은 말"], "sql": "SELECT ...", "rationale": "고른 이유", "unsure": ""}
            고칠 수 없으면 sql 을 빈 문자열로 두고 unsure 에 이유를 적으십시오.""";

    // 예시 기간이 있으면 지표 초안처럼 인덱스 시각 컬럼에 건다 — 질문마다 다른 LLM 이 이 기간을 바꾼다.
    // 없으면 기간을 넣지 않고, 큰 테이블 전체를 훑는지는 실행 전 확인이 판단한다.
    static final String PERIOD_RULE = "- 기간 조건은 [기간 예시] 대로 {table}.{column} 에 적습니다. 이 컬럼은 인덱스 앞 칸이라 WHERE 에서 함수나 계산으로 감싸지 않습니다 (인덱스를 타게).\n"
            + "  예시 기간입니다 — 질문마다 다른 LLM 이 이 기간을 바꿉니다. 결과 비교를 빨리 끝내려고 짧게 둡니다.";
    static final String NO_PERIOD_RULE = "- 기간 조건은 넣지 않습니다 (질문마다 붙습니다).";

    static final String SIMPLE_REASON =
            "단순 집계입니다 — 한 컬럼을 그대로 COUNT · SUM · AVG 하고 묶기만 하는 계산은 질문할 때 LLM 이 바로 만들어 지표로 등록하지 않습니다";
    static final Set<String> GROUPING_FUNCTIONS = Set.of("DATE", "DATE_TRUNC", "ADD_HOURS", "ADD_DAYS", "ADD_MINUTES");

    private final MetaDb db;
    private final Background background;
    private final ContractService contracts;
    private final Connections connections;
    private final MetricService metrics;
    private final TargetDb targetDb;

    public MetricAutoService(MetaDb db, Background background, ContractService contracts, Connections connections,
            MetricService metrics, TargetDb targetDb) {
        this.db = db;
        this.background = background;
        this.contracts = contracts;
        this.connections = connections;
        this.metrics = metrics;
        this.targetDb = targetDb;
    }

    // ── 시작 · 조회 ───────────────────────────────────────────────────────

    /** 실행 기록을 만들고 스레드에서 돌린다. 진행은 getRun 으로 본다. */
    public AutoRun startRun(String systemId, AutoRunRequest req, Llm model) {
        Map<String, Object> contract = contracts.loadContract(systemId);
        List<String> tables = pickTables(contract, req);
        if (tables.isEmpty()) {
            throw new ApiException(400, "지표 후보를 쓸 테이블이 없습니다 — 모든 테이블에 지표가 있습니다. 테이블을 직접 고르십시오");
        }
        String runId = UUID.randomUUID().toString();
        Map<String, Object> options = Dyn.of("per_table", req.perTable(), "allow_full_scan", req.allowFullScan(),
                "picked", req.tables().isEmpty() ? "auto" : "manual");
        db.update("INSERT INTO metric_auto_run (id, system_id, options, tables) VALUES (?, ?, ?, ?)",
                runId, systemId, Json.dumps(options), Json.dumps(tables));

        boolean started = background.start("auto:" + systemId, runId, () -> runAuto(systemId, runId, tables, req, model),
                error -> close(runId, "error", Py.firstLine(error)));
        if (!started) {
            db.update("DELETE FROM metric_auto_run WHERE id = ?", runId);
            throw new ApiException(409, "이 시스템에서 지표 자동 등록이 이미 돌고 있습니다");
        }
        log.info("지표 자동 등록 시작: run={} tables={}", runId, tables);
        AutoRunDetail detail = getRun(systemId, runId);
        return new AutoRun(detail.id(), detail.systemId(), detail.status(), detail.options(), detail.tables(),
                detail.tablesDone(), detail.proposed(), detail.registered(), detail.failed(), detail.skipped(),
                detail.promptTokens(), detail.completionTokens(), detail.error(), detail.startedAt(), detail.finishedAt());
    }

    public List<AutoRun> listRuns(String systemId, int limit) {
        List<AutoRun> runs = new ArrayList<>();
        for (Map<String, Object> r : db.query(
                "SELECT * FROM metric_auto_run WHERE system_id = ? ORDER BY started_at DESC LIMIT ?", systemId, limit)) {
            runs.add(toRun(r));
        }
        return runs;
    }

    public AutoRunDetail getRun(String systemId, String runId) {
        Map<String, Object> row = db.one("SELECT * FROM metric_auto_run WHERE id = ? AND system_id = ?", runId, systemId);
        if (row == null) {
            throw new ApiException(404, "자동 등록 실행을 찾을 수 없습니다");
        }
        List<AutoItem> items = new ArrayList<>();
        for (Map<String, Object> r : db.query("SELECT * FROM metric_auto_item WHERE run_id = ? ORDER BY id", runId)) {
            items.add(toItem(r));
        }
        return AutoRunDetail.of(toRun(row), items);
    }

    /**
     * 고른 테이블을 실제 이름으로. 고르지 않았으면 지표가 없는 테이블 중 나가는 관계(FK)가 많은 것부터 —
     * 관계가 많은 테이블은 대개 차원을 여럿 거느린 팩트 테이블이라 계산할 숫자가 있다.
     */
    static List<String> pickTables(Map<String, Object> contract, AutoRunRequest req) {
        Map<String, String> names = new LinkedHashMap<>();
        for (Map<String, Object> t : Dyn.maps(contract.get("tables"))) {
            names.put(((String) t.get("name")).toLowerCase(), (String) t.get("name"));
        }
        if (!req.tables().isEmpty()) {
            List<String> picked = new ArrayList<>();
            for (String n : req.tables()) {
                String real = names.get(n.toLowerCase());
                if (real == null) {
                    throw new ApiException(400, "메타데이터에 없는 테이블입니다 -> " + n);
                }
                if (!picked.contains(real)) {
                    picked.add(real);
                }
            }
            return picked;
        }
        Set<String> withMetric = new HashSet<>();
        for (Map<String, Object> m : Dyn.maps(contract.get("metrics"))) {
            withMetric.addAll(Dyn.strings(m.get("tables")));
        }
        Map<String, Integer> outgoing = new LinkedHashMap<>();
        for (Map<String, Object> r : Dyn.maps(contract.get("relations"))) {
            outgoing.merge((String) r.get("left_table"), 1, Integer::sum);
        }
        List<String> candidates = new ArrayList<>();
        for (Map<String, Object> t : Dyn.maps(contract.get("tables"))) {
            if (!withMetric.contains((String) t.get("name"))) {
                candidates.add((String) t.get("name"));
            }
        }
        candidates.sort(Comparator.comparing((String n) -> -outgoing.getOrDefault(n, 0)).thenComparing(n -> n));
        return new ArrayList<>(candidates.subList(0, Math.min(req.maxTables(), candidates.size())));
    }

    // ── 실행 ──────────────────────────────────────────────────────────────

    void runAuto(String systemId, String runId, List<String> tables, AutoRunRequest req, Llm model) {
        Map<String, Object> row = connections.row(systemId);
        Set<String> taken = new HashSet<>();
        for (Map<String, Object> r : db.query("SELECT name FROM metric WHERE system_id = ?", systemId)) {
            taken.add(((String) r.get("name")).toLowerCase());
        }
        int promptTokens = 0;
        int completionTokens = 0;
        int done = 0;
        for (String name : tables) {
            done += 1;
            // 테이블마다 다시 읽는다 — 앞 테이블에서 등록한 지표가 [이미 있는 지표] 에 실린다
            Map<String, Object> contract = contracts.loadContract(systemId);
            Map<String, Object> table = null;
            for (Map<String, Object> t : Dyn.maps(contract.get("tables"))) {
                if (t.get("name").equals(name)) {
                    table = t;
                }
            }
            Step step = new Step("지표 자동 등록", model.modelName());
            if (table == null) {
                insertItem(runId, failedItem(name, "llm", "동기화 뒤 사라진 테이블입니다"));
            } else {
                tableRun(systemId, runId, row, contract, table, req, model, step, taken);
            }
            promptTokens += step.promptTokens;
            completionTokens += step.completionTokens;
            progress(runId, done, promptTokens, completionTokens);
        }
        close(runId, "done", null);
        log.info("지표 자동 등록 끝: run={}", runId);
    }

    /** 테이블 하나: 후보를 받고 후보마다 등록을 시도한다 */
    private void tableRun(String systemId, String runId, Map<String, Object> row, Map<String, Object> contract,
            Map<String, Object> table, AutoRunRequest req, Llm model, Step step, Set<String> taken) {
        List<Map<String, Object>> indexes = indexes(row, table);
        Map<String, Object> period = periodExample(row, table, indexes);
        String[] prompt = autoPrompt(contract, table, indexes, req.perTable(), period);
        List<Llm.Message> base = new ArrayList<>(List.of(Llm.Message.system(prompt[0]), Llm.Message.user(prompt[1])));
        Object[] asked;
        try {
            asked = ask(model, base, step);
        } catch (IllegalArgumentException error) {
            insertItem(runId, failedItem((String) table.get("name"), "llm", error.getMessage()));
            return;
        }
        Llm.Reply reply = (Llm.Reply) asked[0];
        @SuppressWarnings("unchecked")
        Map<String, Object> parsed = (Map<String, Object>) asked[1];
        List<Map<String, Object>> proposals = Dyn.maps(parsed.get("metrics"));
        if (proposals.isEmpty()) {
            String skip = Py.text(parsed.get("skip")).strip();
            Map<String, Object> item = failedItem((String) table.get("name"), "llm",
                    skip.isEmpty() ? "LLM 이 지표 후보를 내지 않았습니다" : skip);
            item.put("status", "skipped");
            insertItem(runId, item);
            return;
        }
        Function<String, String> simple = simpleCheck(contract, period);
        List<Llm.Message> withReply = new ArrayList<>(base);
        withReply.add(Llm.Message.assistant(reply.content()));
        for (Map<String, Object> p : proposals.subList(0, Math.min(req.perTable(), proposals.size()))) {
            Map<String, Object> item = candidate(systemId, row, model, withReply, p, step, taken, (String) table.get("name"),
                    req.allowFullScan(), simple);
            insertItem(runId, item);
        }
    }

    /** 후보 목록을 받는다 → [응답, JSON]. 형식이 틀리면 한 번 더 묻는다. */
    private static Object[] ask(Llm model, List<Llm.Message> messages, Step step) {
        String last = null;
        List<Llm.Message> current = new ArrayList<>(messages);
        for (int attempt = 1; attempt <= 2; attempt++) {
            Llm.Reply reply = LlmCalls.chat(model, current, step, attempt);
            try {
                return new Object[] {reply, LlmCalls.parseJson(reply.content())};
            } catch (IllegalArgumentException error) {
                current.add(Llm.Message.assistant(reply.content()));
                current.add(Llm.Message.user("형식이 틀렸습니다: " + error.getMessage() + ". 설명 없이 JSON 객체 하나만 다시 답하십시오."));
                last = error.getMessage();
            }
        }
        throw new IllegalArgumentException("지표 후보 응답을 읽지 못했습니다: " + last);
    }

    /** 후보 하나를 등록까지 밀어 본다 → 기록할 항목. simple(sql) 이 이유를 돌려주면 단순 집계라 건너뛴다. */
    Map<String, Object> candidate(String systemId, Map<String, Object> row, Llm model, List<Llm.Message> messages,
            Map<String, Object> proposal, Step step, Set<String> taken, String tableName, boolean allowFullScan,
            Function<String, String> simple) {
        Map<String, Object> item = failedItem(tableName, null, "");
        Map<String, Object> current = proposal;
        List<Llm.Message> history = new ArrayList<>(messages);
        for (int attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
            fill(item, current);
            item.put("attempts", attempt);
            if (Py.text(item.get("sql")).isEmpty()) {
                item.put("stage", "llm");
                String unsure = Py.text(current.get("unsure")).strip();
                item.put("reason", unsure.isEmpty() ? "SQL 이 비었습니다" : unsure);
                return item;
            }
            String name = (String) item.get("name");
            if (name.isEmpty()) {
                item.put("stage", "llm");
                item.put("reason", "지표 이름이 비었습니다");
                return item;
            }
            if (taken.contains(name.toLowerCase())) {
                item.put("status", "skipped");
                item.put("stage", "duplicate");
                item.put("reason", "'" + name + "' 이름의 지표가 이미 있습니다");
                return item;
            }
            String reason = simple != null ? simple.apply((String) item.get("sql")) : null;
            if (reason != null) {
                // 대상 DB 에서 돌리기 전에 거른다. 지표가 아니라는 판단이라 고쳐 쓰게 하지 않는다
                item.put("status", "skipped");
                item.put("stage", "simple");
                item.put("reason", reason);
                return item;
            }
            item.put("full_scans", new ArrayList<String>());
            MetricCheck check = metrics.checkMetric(systemId, (String) item.get("sql"), guard(row, item, allowFullScan));
            item.put("check", check);
            Dyn.items(item.get("tries")).add(Dyn.of("sql", item.get("sql"), "stage", check.stage, "error", check.error));
            if (check.error == null) {
                break;
            }
            item.put("stage", check.stage);
            item.put("reason", check.error);
            if (attempt == MAX_ATTEMPTS) {
                return item;
            }
            String stage = MetricService.STAGE_LABEL.getOrDefault(check.stage, "확인 실패");
            history.add(Llm.Message.user(FIX_REQUEST.replace("{name}", name).replace("{stage}", stage)
                    .replace("{error}", check.error)));
            Llm.Reply reply = LlmCalls.chat(model, history, step, attempt + 1);
            history.add(Llm.Message.assistant(reply.content()));
            try {
                current = one(LlmCalls.parseJson(reply.content()));
            } catch (IllegalArgumentException error) {
                item.put("stage", "llm");
                item.put("reason", "고친 응답을 읽지 못했습니다: " + error.getMessage());
                return item;
            }
        }

        Metric metric;
        try {
            metric = metrics.createMetric(systemId, new MetricInput((String) item.get("name"), (String) item.get("description"),
                    Dyn.strings(item.get("synonyms")), (String) item.get("sql")).validated());
        } catch (ApiException error) {
            item.put("stage", "save");
            item.put("reason", error.detail());
            return item;
        }
        taken.add(((String) item.get("name")).toLowerCase());
        item.put("status", "registered");
        item.put("stage", null);
        item.put("reason", "");
        item.put("metric_id", metric.id());
        log.info("지표 자동 등록: {} ({}, 시도 {})", item.get("name"), tableName, item.get("attempts"));
        return item;
    }

    /** 후보 SQL 이 단순 집계인지 보는 함수 simple(sql) → 이유 또는 null. 옮기지 못하는 SQL 은 등록 확인에 맡긴다. */
    static Function<String, String> simpleCheck(Map<String, Object> contract, Map<String, Object> period) {
        Set<String> needsCalc = new HashSet<>();
        for (Map<String, Object> t : Dyn.maps(contract.get("tables"))) {
            for (Map<String, Object> c : Dyn.maps(t.get("columns"))) {
                if (Dyn.bool(c, "needs_calc")) {
                    needsCalc.add(((String) c.get("name")).toLowerCase());
                }
            }
        }
        String periodColumn = period != null ? ((String) period.get("column")).toLowerCase() : null;
        return sql -> {
            Map<String, Object> ast;
            try {
                ast = SqlAst.parseSql(sql, contract).ast();
            } catch (IllegalArgumentException error) {
                return null;
            }
            return isSimple(ast, needsCalc, periodColumn) ? SIMPLE_REASON : null;
        };
    }

    /**
     * 단순 집계: 집계마다 컬럼 하나(또는 COUNT(*))를 그대로 쓰고, 나머지는 컬럼이나 날짜 묶음이며,
     * 조건은 예시 기간과 NULL 여부뿐이다. JOIN 은 따지지 않는다 — 묶을 컬럼을 가져오는 JOIN 도 질문 LLM 이 만든다.
     * 계산 필요 컬럼의 집계, 집계끼리의 식, CASE, 값 조건(업무 규칙), HAVING, 하위 쿼리가 있으면 단순하지 않다.
     */
    static boolean isSimple(Map<String, Object> ast, Set<String> needsCalc, String periodColumn) {
        if (!(ast.get("from") instanceof String) || Py.truthy(ast.get("having"))) {
            return false;
        }
        for (Map<String, Object> j : Dyn.maps(ast.get("joins"))) {
            if (!(j.get("table") instanceof String)) {
                return false;
            }
        }
        boolean aggregated = false;
        for (Map<String, Object> s : Dyn.maps(ast.get("select"))) {
            Map<String, Object> expr = Dyn.map(s.get("expr")) == null ? new LinkedHashMap<>() : Dyn.map(s.get("expr"));
            if (plainAggregate(expr, needsCalc)) {
                aggregated = true;
            } else if (!grouping(expr)) {
                return false;
            }
        }
        for (Object g : Dyn.items(ast.get("group_by"))) {
            if (!grouping(Dyn.map(g))) {
                return false;
            }
        }
        for (Map<String, Object> cond : flatConditions(Dyn.maps(ast.get("where")))) {
            if ("is_null".equals(cond.get("op")) || "is_not_null".equals(cond.get("op"))) {
                continue;
            }
            String column = columnName(Dyn.map(cond.get("expr")));
            if (column == null || !column.equals(periodColumn)) {
                return false;
            }
        }
        return aggregated;
    }

    private static boolean plainAggregate(Map<String, Object> expr, Set<String> needsCalc) {
        if (!Compiler.AGGREGATES.contains(String.valueOf(expr.get("fn")))) {
            return false;
        }
        List<Object> args = Dyn.items(expr.get("args"));
        if (args.isEmpty()) {
            return true;  // COUNT(*)
        }
        String column = args.size() == 1 ? columnName(Dyn.map(args.get(0))) : null;
        return column != null && !needsCalc.contains(column);
    }

    /** 컬럼, 또는 컬럼을 날짜로 자르거나 시차만큼 옮긴 것 */
    private static boolean grouping(Map<String, Object> expr) {
        if (expr == null) {
            return false;
        }
        if (expr.containsKey("col")) {
            return !"*".equals(expr.get("col"));
        }
        if (expr.containsKey("num") || expr.containsKey("str")) {
            return true;
        }
        if (GROUPING_FUNCTIONS.contains(String.valueOf(expr.get("fn")))) {
            for (Object a : Dyn.items(expr.get("args"))) {
                if (!grouping(Dyn.map(a))) {
                    return false;
                }
            }
            return true;
        }
        return false;
    }

    private static String columnName(Map<String, Object> expr) {
        if (expr == null || !expr.containsKey("col")) {
            return null;
        }
        String[] parts = String.valueOf(expr.get("col")).split("\\.", -1);
        return parts[parts.length - 1].toLowerCase();
    }

    /** AND · OR 을 펼친 조건 목록 */
    private static List<Map<String, Object>> flatConditions(List<Map<String, Object>> conds) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Map<String, Object> c : conds) {
            if (c.containsKey("and")) {
                out.addAll(flatConditions(Dyn.maps(c.get("and"))));
            } else if (c.containsKey("or")) {
                out.addAll(flatConditions(Dyn.maps(c.get("or"))));
            } else {
                out.add(c);
            }
        }
        return out;
    }

    /** 고친 응답은 지표 하나다. 목록으로 답했으면 첫 지표를 쓴다. */
    private static Map<String, Object> one(Map<String, Object> parsed) {
        List<Object> metrics = Dyn.list(parsed.get("metrics"));
        if (metrics != null && !metrics.isEmpty() && metrics.get(0) instanceof Map) {
            return Dyn.map(metrics.get(0));
        }
        return parsed;
    }

    private static void fill(Map<String, Object> item, Map<String, Object> p) {
        String sql = Py.text(p.get("sql")).strip();
        while (sql.endsWith(";")) {
            sql = sql.substring(0, sql.length() - 1);
        }
        item.put("sql", sql.strip());
        item.put("name", Py.squeeze(Py.truthy(p.get("name")) ? Py.text(p.get("name")) : Py.text(item.get("name"))));
        item.put("description", Py.squeeze(Py.truthy(p.get("description")) ? Py.text(p.get("description")) : Py.text(item.get("description"))));
        item.put("rationale", Py.squeeze(Py.truthy(p.get("rationale")) ? Py.text(p.get("rationale")) : Py.text(item.get("rationale"))));
        Object synonyms = p.get("synonyms");
        List<Object> list = synonyms instanceof String s ? List.of(s) : Dyn.items(synonyms);
        List<String> cleaned = new ArrayList<>();
        for (Object s : list) {
            String text = Py.text(s).strip();
            if (!text.isEmpty() && !cleaned.contains(text) && !text.equals(item.get("name"))) {
                cleaned.add(text);
            }
        }
        item.put("synonyms", cleaned);
    }

    /** 등록 확인이 대상 DB 에서 실행하기 직전에 부르는 검사 (지표 초안과 같은 기준) */
    private BiFunction<String, String, String> guard(Map<String, Object> row, Map<String, Object> item, boolean allowFullScan) {
        return (original, compiled) -> {
            List<String> positional = SqlAst.positionalRefs(original);
            if (!positional.isEmpty()) {
                return String.join(", ", positional) + " 에 순번(1, 2 …)을 썼습니다 — 순번 대신 컬럼이나 식을 그대로 적으십시오";
            }
            List<String> scans = planFullScans(row, original, compiled);
            item.put("full_scans", scans);
            if (!scans.isEmpty() && !allowFullScan) {
                return "실행 계획이 " + String.join(", ", scans) + " 전체를 훑어 결과 비교를 하지 않았습니다 — "
                        + "인덱스가 있는 컬럼에 조건을 거십시오 (테스트 DB 면 '전체 훑기 허용' 으로 다시 돌리십시오)";
            }
            return null;
        };
    }

    /** SQL 들의 실행 계획(EXPLAIN 만)에서 전체를 훑는 큰 테이블. 못 보면 빈 목록이다. */
    public List<String> planFullScans(Map<String, Object> row, String... sqls) {
        return MetricDraftService.planFullScans(targetDb, row, PLAN_TIMEOUT_S, sqls);
    }

    private List<Map<String, Object>> indexes(Map<String, Object> row, Map<String, Object> table) {
        Set<String> names = new HashSet<>();
        for (Map<String, Object> c : Dyn.maps(table.get("columns"))) {
            names.add((String) c.get("name"));
        }
        try (TargetSession cur = targetDb.readOnly(row, PLAN_TIMEOUT_S)) {
            return Catalog.readIndexes(cur, row, table, names);
        } catch (SQLException | RuntimeException error) {
            log.warn("{} 인덱스를 읽지 못했습니다: {}", table.get("name"), Py.firstLine(error));
            return new ArrayList<>();
        }
    }

    /**
     * 인덱스 앞 칸인 날짜 · 시각 컬럼의 가장 최근 값으로 예시 기간을 정한다 → {column, latest, start, end} 또는 null.
     *
     * 최근 값은 그 인덱스를 거꾸로 한 행만 읽는다. 실행 계획이 전체를 훑으면 읽지 않고 다음 후보를 본다.
     * 기간은 저장된 값 기준의 날짜 [start, end) 다 — 최근 날을 끝으로 DRAFT_DAYS 일.
     */
    Map<String, Object> periodExample(Map<String, Object> row, Map<String, Object> table, List<Map<String, Object>> indexes) {
        Map<String, String> kinds = new LinkedHashMap<>();
        for (Map<String, Object> c : Dyn.maps(table.get("columns"))) {
            kinds.put((String) c.get("name"), Compiler.temporalKind(Dyn.text(c, "type"), (String) row.get("driver")));
        }
        List<String> heads = new ArrayList<>();
        for (String kind : List.of("datetime", "date")) {  // 시각 컬럼이 날짜 컬럼보다 기록 시점에 가깝다
            for (Map<String, Object> ix : indexes) {
                String head = Dyn.strings(ix.get("columns")).get(0);
                if (kind.equals(kinds.get(head)) && !heads.contains(head)) {
                    heads.add(head);
                }
            }
        }
        if (heads.isEmpty()) {
            return null;
        }
        Dialect d = new Dialect((String) row.get("driver"));
        try (TargetSession cur = targetDb.readOnly(row, PLAN_TIMEOUT_S)) {
            for (String column : heads) {
                String sql = "SELECT " + d.q(column) + " FROM " + d.table((String) table.get("name"), Dyn.text(table, "schema"))
                        + " WHERE " + d.q(column) + " IS NOT NULL ORDER BY " + d.q(column) + " DESC " + d.limit(1);
                if (!Plans.fullScans(cur, d, sql).isEmpty()) {
                    continue;
                }
                List<Object> found = cur.one(sql);
                if (found == null || found.get(0) == null) {
                    continue;
                }
                Object latest = found.get(0);
                LocalDate day;
                if (latest instanceof LocalDateTime t) {
                    day = t.toLocalDate();
                } else if (latest instanceof OffsetDateTime t) {
                    day = t.toLocalDate();
                } else if (latest instanceof LocalDate t) {
                    day = t;
                } else {
                    continue;
                }
                LocalDate start = day.minusDays(MetricDraftService.DRAFT_DAYS - 1);
                return Dyn.of("column", column, "latest", latestText(latest), "start", Times.day(start), "end",
                        Times.day(day.plusDays(1)));
            }
        } catch (SQLException | RuntimeException error) {
            log.warn("{} 의 최근 시각을 읽지 못했습니다: {}", table.get("name"), Py.firstLine(error));
        }
        return null;
    }

    /** str(시각) — 파이썬처럼 날짜와 시각 사이에 공백 */
    private static String latestText(Object latest) {
        if (latest instanceof LocalDateTime t) {
            return Times.iso(t).replace('T', ' ');
        }
        if (latest instanceof OffsetDateTime t) {
            return Times.iso(t).replace('T', ' ');
        }
        return Times.day((LocalDate) latest);
    }

    // ── 프롬프트 ───────────────────────────────────────────────────────────

    /** → [시스템 프롬프트, 사용자 메시지]. period 는 periodExample 의 예시 기간 (없으면 기간 조건을 넣지 않게 한다) */
    static String[] autoPrompt(Map<String, Object> contract, Map<String, Object> table, List<Map<String, Object>> indexes,
            int count, Map<String, Object> period) {
        String driver = Py.truthy(contract.get("driver")) ? (String) contract.get("driver") : "postgresql";
        String domain = Py.truthy(contract.get("domain")) ? "[업무 영역]\n" + contract.get("domain") + "\n" : "";
        String periodRule = period != null
                ? PERIOD_RULE.replace("{table}", (String) table.get("name")).replace("{column}", (String) period.get("column"))
                : NO_PERIOD_RULE;
        String system = AUTO_SYSTEM.replace("{name}", Py.text(contract.get("name"))).replace("{driver}", driver)
                .replace("{domain}", domain).replace("{count}", String.valueOf(count)).replace("{period_rule}", periodRule);

        String note = ContractService.firstText(table.get("purpose"), table.get("description"));
        List<String> lines = new ArrayList<>();
        lines.add(("[대상 테이블]\n[" + table.get("name") + "] " + Py.squeeze(note)).stripTrailing());
        for (Map<String, Object> c : Dyn.maps(table.get("columns"))) {
            lines.add(columnLine(c, driver));
        }

        lines.add("\n[인덱스]");
        for (Map<String, Object> ix : indexes) {
            lines.add("  - " + ix.get("name") + " (" + String.join(", ", Dyn.strings(ix.get("columns"))) + ")");
        }
        if (indexes.isEmpty()) {
            lines.add("  (없음)");
        }

        if (period != null) {
            String c = (String) period.get("column");
            lines.add("\n[기간 예시] 가장 최근 값 " + period.get("latest") + " 기준 " + MetricDraftService.DRAFT_DAYS + "일");
            lines.add("  WHERE " + table.get("name") + "." + c + " >= '" + period.get("start") + "' AND " + table.get("name") + "."
                    + c + " < '" + period.get("end") + "'");
        }

        Map<String, Map<String, Object>> byName = new LinkedHashMap<>();
        for (Map<String, Object> t : Dyn.maps(contract.get("tables"))) {
            byName.put((String) t.get("name"), t);
        }
        List<String> related = new ArrayList<>();
        List<String> pairs = new ArrayList<>();
        for (Map<String, Object> r : Dyn.maps(contract.get("relations"))) {
            String other;
            if (r.get("left_table").equals(table.get("name"))) {
                other = (String) r.get("right_table");
            } else if (r.get("right_table").equals(table.get("name"))) {
                other = (String) r.get("left_table");
            } else {
                continue;
            }
            pairs.add("  - " + r.get("left_table") + "." + r.get("left_column") + " = " + r.get("right_table") + "."
                    + r.get("right_column"));
            if (!other.equals(table.get("name")) && !related.contains(other)) {
                related.add(other);
            }
        }
        lines.add("\n[관계]");
        if (pairs.isEmpty()) {
            lines.add("  (없음)");
        } else {
            lines.addAll(pairs);
        }
        List<String> shown = related.subList(0, Math.min(MAX_RELATED, related.size()));
        if (!shown.isEmpty()) {
            lines.add("\n[이어진 테이블]");
            for (String name : shown) {
                Map<String, Object> t = byName.get(name);
                List<Map<String, Object>> columns = Dyn.maps(t.get("columns"));
                List<String> cols = new ArrayList<>();
                for (Map<String, Object> c : columns.subList(0, Math.min(MAX_RELATED_COLUMNS, columns.size()))) {
                    cols.add(c.get("name") + "(" + Dyn.text(c, "type") + ")");
                }
                String more = columns.size() > MAX_RELATED_COLUMNS ? " 외 " + (columns.size() - MAX_RELATED_COLUMNS) + "개" : "";
                String desc = Py.squeeze(ContractService.firstText(t.get("purpose"), t.get("description")));
                lines.add("- " + name + (desc.isEmpty() ? "" : ": " + desc) + "\n  컬럼: " + String.join(", ", cols) + more);
            }
            if (related.size() > shown.size()) {
                lines.add("(이어진 테이블 " + (related.size() - shown.size()) + "개 생략)");
            }
        }

        List<Map<String, Object>> same = new ArrayList<>();
        List<Map<String, Object>> others = new ArrayList<>();
        for (Map<String, Object> m : Dyn.maps(contract.get("metrics"))) {
            (Dyn.strings(m.get("tables")).contains((String) table.get("name")) ? same : others).add(m);
        }
        lines.add("\n[이미 있는 지표]");
        for (Map<String, Object> m : same) {
            String desc = Py.squeeze(m.get("description"));
            lines.add("- " + m.get("name") + ": " + (desc.isEmpty() ? "(설명 없음)" : desc) + "\n  SQL: " + Py.squeeze(m.get("sql")));
        }
        for (Map<String, Object> m : others.subList(0, Math.min(MAX_OTHER_METRICS, others.size()))) {
            lines.add("- " + m.get("name") + " (" + String.join(", ", Dyn.strings(m.get("tables"))) + ")");
        }
        if (same.isEmpty() && others.isEmpty()) {
            lines.add("  (없음)");
        }
        return new String[] {system, String.join("\n", lines)};
    }

    public static String columnLine(Map<String, Object> c, String driver) {
        String note = Py.squeeze(c.get("description"));
        List<String> marks = new ArrayList<>();
        if (Dyn.bool(c, "is_pk")) {
            marks.add("[PK]");
        }
        if (Py.truthy(c.get("utc_offset")) && "datetime".equals(Compiler.temporalKind(Dyn.text(c, "type"), driver))) {
            marks.add("[UTC " + Py.signed(Dyn.integer(c, "utc_offset")) + "시간]");
        }
        if (Dyn.bool(c, "needs_calc")) {
            marks.add("[계산 필요]");
        }
        List<String> parts = new ArrayList<>();
        parts.add(note);
        parts.addAll(marks);
        String tail = String.join(" ", parts).strip();
        return "  - " + c.get("name") + " (" + Dyn.text(c, "type") + ")" + (tail.isEmpty() ? "" : ": " + tail);
    }

    // ── 기록 ─────────────────────────────────────────────────────────────

    static Map<String, Object> failedItem(String tableName, String stage, String reason) {
        return Dyn.of("table_name", tableName, "name", "", "description", "", "synonyms", new ArrayList<>(), "sql", "",
                "rationale", "", "status", "failed", "stage", stage, "reason", reason, "attempts", 0,
                "tries", new ArrayList<>(), "check", null, "full_scans", new ArrayList<>(), "metric_id", null);
    }

    private void insertItem(String runId, Map<String, Object> item) {
        Object check = item.get("check");
        db.update("""
                INSERT INTO metric_auto_item (run_id, table_name, name, description, synonyms, sql, rationale, status, stage,
                                              reason, attempts, tries, check_result, full_scans, metric_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, runId, item.get("table_name"), item.get("name"), item.get("description"), Json.dumps(item.get("synonyms")),
                item.get("sql"), item.get("rationale"), item.get("status"), item.get("stage"), item.get("reason"),
                item.get("attempts"), Json.dumps(item.get("tries")), check != null ? Json.dumps(Json.plain(check)) : null,
                Json.dumps(item.get("full_scans")), item.get("metric_id"));
    }

    private void progress(String runId, int tablesDone, int promptTokens, int completionTokens) {
        db.update("""
                UPDATE metric_auto_run r
                   SET tables_done = ?, prompt_tokens = ?, completion_tokens = ?,
                       proposed   = (SELECT count(*) FROM metric_auto_item i WHERE i.run_id = r.id AND i.attempts > 0),
                       registered = (SELECT count(*) FROM metric_auto_item i WHERE i.run_id = r.id AND i.status = 'registered'),
                       failed     = (SELECT count(*) FROM metric_auto_item i WHERE i.run_id = r.id AND i.status = 'failed'),
                       skipped    = (SELECT count(*) FROM metric_auto_item i WHERE i.run_id = r.id AND i.status = 'skipped')
                 WHERE r.id = ?
                """, tablesDone, promptTokens, completionTokens, runId);
    }

    private void close(String runId, String status, String error) {
        db.update("UPDATE metric_auto_run SET status = ?, error = ?, finished_at = now() WHERE id = ?", status, error, runId);
    }

    private AutoRun toRun(Map<String, Object> r) {
        return new AutoRun(
                String.valueOf(r.get("id")),
                String.valueOf(r.get("system_id")),
                RunRecords.status(r, background),
                r.get("options") == null ? new LinkedHashMap<>() : Dyn.map(r.get("options")),
                Dyn.items(r.get("tables")),
                RunRecords.integer(r, "tables_done"),
                RunRecords.integer(r, "proposed"),
                RunRecords.integer(r, "registered"),
                RunRecords.integer(r, "failed"),
                RunRecords.integer(r, "skipped"),
                RunRecords.integer(r, "prompt_tokens"),
                RunRecords.integer(r, "completion_tokens"),
                (String) r.get("error"),
                RunRecords.started(r),
                RunRecords.finished(r));
    }

    private static AutoItem toItem(Map<String, Object> r) {
        List<AutoTry> tries = new ArrayList<>();
        for (Map<String, Object> t : Dyn.maps(r.get("tries"))) {
            tries.add(new AutoTry((String) t.get("sql"), (String) t.get("stage"), (String) t.get("error")));
        }
        return new AutoItem(
                ((Number) r.get("id")).longValue(),
                (String) r.get("table_name"),
                (String) r.get("name"),
                (String) r.get("description"),
                Dyn.items(r.get("synonyms")),
                (String) r.get("sql"),
                (String) r.get("rationale"),
                (String) r.get("status"),
                (String) r.get("stage"),
                (String) r.get("reason"),
                RunRecords.integer(r, "attempts"),
                tries,
                r.get("check_result") != null ? Json.convert(r.get("check_result"), MetricCheck.class) : null,
                Dyn.items(r.get("full_scans")),
                r.get("metric_id") == null ? null : String.valueOf(r.get("metric_id")),
                Times.iso((OffsetDateTime) r.get("created_at")));
    }
}
