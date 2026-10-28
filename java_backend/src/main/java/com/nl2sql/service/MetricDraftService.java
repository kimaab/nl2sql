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
import java.util.Objects;
import java.util.Set;
import java.util.function.BiFunction;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import com.nl2sql.common.ApiException;
import com.nl2sql.common.Dyn;
import com.nl2sql.common.Py;
import com.nl2sql.common.Times;
import com.nl2sql.compiler.Compiler;
import com.nl2sql.compiler.Dialect;
import com.nl2sql.db.Catalog;
import com.nl2sql.db.TargetDb;
import com.nl2sql.db.TargetSession;
import com.nl2sql.llm.Llm;
import com.nl2sql.llm.LlmCalls;
import com.nl2sql.llm.Step;
import com.nl2sql.model.MetricModels.MetricDraft;
import com.nl2sql.model.MetricModels.MetricDraftRequest;
import com.nl2sql.model.MetricModels.ValueSample;
import com.nl2sql.sqlast.SqlAst;

/**
 * 지표 초안: 지표가 없어 멈춘 질문에서 LLM 이 지표 SQL 초안을 쓴다. 저장하지 않는다 — 사람이 확인한 뒤 등록한다 (metric_draft.py).
 *
 * 재료는 대상 테이블의 컬럼(계산 필요 · UTC 표시), 인덱스, 같은 테이블에 이미 있는 지표, 대상 컬럼의 실제 값 샘플이다.
 * 값 샘플은 인덱스를 타는 조회로만 뽑는다 — 인덱스 앞 칸에 값이나 범위를 걸고, 인덱스 칸 순서로 몇 행만 읽는다.
 * 지표가 필요한 테이블은 대개 로그처럼 큰 테이블이라, 인덱스 없이 훑으면 데이터가 적어도 운영 DB 에 부담이 된다.
 * 초안 SQL 은 등록과 같은 확인(AST 변환 → 컴파일 → 결과 비교)을 거치고, 실패하면 오류를 붙여 다시 쓰게 한다.
 */
@Service
public class MetricDraftService {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.draft");

    static final int MAX_ATTEMPTS = 3;
    static final int SAMPLE_TIMEOUT_S = 10;  // 샘플 조회 하나의 제한. 인덱스를 타면 금방 끝난다
    static final int SAMPLE_DAYS = 3;  // 날짜별 첫 값 · 마지막 값을 볼 날 수 (누적값이 날을 넘어 이어지는지, 리셋되는지)
    static final int RECENT_ROWS = 10;  // 가장 최근 몇 행 (기록 간격과 값이 커지는 모양)
    static final int CANDIDATE_ROWS = 50;  // 묶음(차량) 후보를 고를 때 읽는 최근 행 수
    static final int CANDIDATES = 5;  // 그중 값이 바뀌는 묶음을 찾아볼 개수
    static final int SCAN_ROWS = 300;  // 묶음(장비) 인덱스가 없을 때 최근 행을 이만큼 읽어 묶음 하나를 고른다
    static final int SHOW_ROWS = 30;
    public static final int DRAFT_DAYS = 3;  // 초안 SQL 의 예시 기간. 등록 확인이 대상 DB 에서 원본과 옮긴 SQL 을 모두 돌리므로 짧게

    static final String DRAFT_SYSTEM = """
            당신은 '{name}' 데이터베이스({driver})에 등록할 지표의 SQL 초안을 쓰는 역할입니다.
            지표는 사람이 확인한 뒤 등록하고, 이후 질문마다 다른 LLM 이 이 SQL 의 기간·조건·묶음만 바꿔 씁니다.
            {domain}
            [규칙]
            - [같은 테이블 지표] 가 있으면 그 SQL 의 묶음 단위(예: 일 × 장비)와 시각 처리 방식을 따릅니다.
            - 질문의 기간·개수·정렬은 지표에 넣지 않습니다. 지표는 계산의 기본 단위입니다 (예: '최근 7일 합계' 를 물어도 지표는 일별 값).
            - [계산 필요] 컬럼은 그대로 SUM·AVG 하지 않습니다. 컬럼 설명과 [값 샘플] 로 계산 방식을 정합니다
              (예: 날을 넘어 계속 커지는 누적값이면 하루의 MAX - MIN).
            - 기간 조건은 예시 값으로 {anchor} 까지 {days}일을 적습니다.
            - 기간 조건은 [인덱스] 에 있는 시각 컬럼에 걸고, WHERE 에서 그 컬럼을 함수나 계산으로 감싸지 않습니다 (인덱스를 타게).
            - [UTC +n시간] 컬럼은 UTC 로 저장된 시각입니다. 현지 날짜로 묶을 때는 SELECT·GROUP BY 에서 '컬럼 + n시간' 으로 적고,
              WHERE 의 기간 조건은 컬럼을 그대로 두고 UTC 값으로 적습니다.
            - GROUP BY · ORDER BY 에 순번(1, 2)을 쓰지 않습니다. SELECT 의 식이나 컬럼을 그대로 적습니다.
            - SELECT 문 하나만 씁니다. 윈도 함수는 ROW_NUMBER 만, DISTINCT ON · OFFSET · 셀프 조인 · 하위 쿼리 JOIN 은 쓰지 않습니다.
            - name 은 [빠진 계산] 의 이름을 그대로 쓰는 것을 기본으로 합니다.
            - description 에는 무엇을 어떻게 계산하는지 적습니다. 질문을 받은 LLM 이 이 지표가 맞는지 판단할 때 읽습니다.
            - assumptions 에는 사람이 확인해야 할 가정을 적습니다 (예: 하루 중 누적값이 리셋되지 않는다).
            - 계산 방식을 정할 수 없으면 sql 을 빈 문자열로 두고 unsure 에 이유를 적습니다.
            - 설명 없이 JSON 객체 하나로만 답합니다:
              {"sql": "SELECT ...", "name": "지표 이름", "synonyms": ["같은 말"], "description": "계산 설명",
                "assumptions": ["확인할 가정"], "unsure": ""}""";

    private final ContractService contracts;
    private final MetricService metrics;
    private final TargetDb targetDb;

    public MetricDraftService(ContractService contracts, MetricService metrics, TargetDb targetDb) {
        this.contracts = contracts;
        this.metrics = metrics;
        this.targetDb = targetDb;
    }

    /** 초안 하나를 쓴다. row 는 대상 DB 접속 정보 (Connections.row). */
    public MetricDraft draftMetric(String systemId, MetricDraftRequest req, Llm model, Map<String, Object> row) {
        Map<String, Object> contract = contracts.loadContract(systemId, true);
        Map<String, Object> table = null;
        for (Map<String, Object> t : Dyn.maps(contract.get("tables"))) {
            if (t.get("name").equals(req.missing().table())) {
                table = t;
                break;
            }
        }
        if (table == null) {
            throw new ApiException(400, "메타데이터에 없는 테이블입니다 -> " + req.missing().table());
        }

        Object[] collected = collect(row, table, req.missing().column());
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> indexes = (List<Map<String, Object>>) collected[0];
        ValueSample sample = (ValueSample) collected[1];
        String[] prompt = draftPrompt(contract, table, req, indexes, sample);
        List<Llm.Message> messages = new ArrayList<>(List.of(Llm.Message.system(prompt[0]), Llm.Message.user(prompt[1])));
        Step step = new Step("draft", model.modelName());
        MetricDraft draft = new MetricDraft();
        draft.sample = sample;
        for (int attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
            Llm.Reply reply = LlmCalls.chat(model, messages, step, attempt);
            draft.attempts = attempt;
            Map<String, Object> parsed;
            try {
                parsed = LlmCalls.parseJson(reply.content());
            } catch (IllegalArgumentException error) {
                messages.add(Llm.Message.assistant(reply.content()));
                messages.add(Llm.Message.user("형식이 틀렸습니다: " + error.getMessage() + ". 설명 없이 JSON 객체 하나만 다시 답하십시오."));
                continue;
            }
            fill(draft, parsed);
            if (draft.unsure != null && draft.sql.isEmpty()) {
                break;
            }
            if (draft.sql.isEmpty()) {
                messages.add(Llm.Message.assistant(reply.content()));
                messages.add(Llm.Message.user("sql 이 비었습니다. SQL 을 적거나, 정할 수 없으면 unsure 에 이유를 적으십시오."));
                continue;
            }
            // 대상 DB 에서 실행하기 전에 순번 참조와 실행 계획을 본다 — 걸리면 실행하지 않고 고쳐 쓰게 한다
            draft.fullScans = new ArrayList<>();
            draft.check = metrics.checkMetric(systemId, draft.sql, guard(row, draft));
            if (draft.check.error == null) {
                break;
            }
            String stage = MetricService.STAGE_LABEL.getOrDefault(draft.check.stage, "확인 실패");
            messages.add(Llm.Message.assistant(reply.content()));
            messages.add(Llm.Message.user(stage + ": " + draft.check.error + "\n고쳐서 JSON 으로 다시 답하십시오."));
        }
        log.info("지표 초안 {}.{}: attempts={} ok={} unsure={}", table.get("name"), req.missing().column(), draft.attempts,
                draft.check != null && draft.check.error == null, draft.unsure);
        return draft;
    }

    /** 등록 확인이 대상 DB 에서 실행하기 직전에 부르는 검사. 실행하면 안 되는 이유를 돌려준다. */
    private BiFunction<String, String, String> guard(Map<String, Object> row, MetricDraft draft) {
        return (original, compiled) -> {
            List<String> positional = SqlAst.positionalRefs(original);
            if (!positional.isEmpty()) {
                return String.join(", ", positional) + " 에 순번(1, 2 …)을 썼습니다 — 순번 대신 컬럼이나 식을 그대로 적으십시오";
            }
            // 원본은 등록 확인에서, 옮긴 SQL 은 질문마다 실행된다 — 둘 다 본다
            draft.fullScans = planFullScans(targetDb, row, SAMPLE_TIMEOUT_S, original, compiled);
            if (!draft.fullScans.isEmpty()) {
                return "실행 계획이 " + String.join(", ", draft.fullScans) + " 전체를 훑어 실행하지 않았습니다 — "
                        + "기간 조건을 인덱스가 있는 시각 컬럼에 함수나 계산 없이 거십시오";
            }
            return null;
        };
    }

    /** SQL 들의 실행 계획(EXPLAIN 만, 실행하지 않는다)에서 전체를 훑는 큰 테이블. 못 보면 빈 목록이다. */
    static List<String> planFullScans(TargetDb targetDb, Map<String, Object> row, int timeoutS, String... sqls) {
        List<String> found = new ArrayList<>();
        try (TargetSession cur = targetDb.readOnly(row, timeoutS)) {
            for (String sql : sqls) {
                for (String name : Plans.fullScans(cur, new Dialect((String) row.get("driver")), sql)) {
                    if (!found.contains(name)) {
                        found.add(name);
                    }
                }
            }
        } catch (SQLException | RuntimeException error) {
            log.warn("실행 계획을 보지 못했습니다: {}", Py.firstLine(error));
        }
        return found;
    }

    private static void fill(MetricDraft draft, Map<String, Object> parsed) {
        String sql = Py.text(parsed.get("sql")).strip();
        while (sql.endsWith(";")) {
            sql = sql.substring(0, sql.length() - 1);
        }
        draft.sql = sql.strip();
        draft.name = Py.text(parsed.get("name")).strip();
        draft.description = Py.squeeze(Py.text(parsed.get("description")));
        String unsure = Py.text(parsed.get("unsure")).strip();
        draft.unsure = unsure.isEmpty() ? null : unsure;
        draft.synonyms = texts(parsed.get("synonyms"));
        draft.assumptions = texts(parsed.get("assumptions"));
    }

    static List<String> texts(Object value) {
        List<Object> items = value instanceof String s ? List.of(s) : Dyn.items(value);
        List<String> out = new ArrayList<>();
        for (Object v : items) {
            String text = Py.text(v).strip();
            if (!text.isEmpty() && !out.contains(text)) {
                out.add(text);
            }
        }
        return out;
    }

    // ── 프롬프트 ───────────────────────────────────────────────────────────

    /** → [시스템 프롬프트, 사용자 메시지] */
    static String[] draftPrompt(Map<String, Object> contract, Map<String, Object> table, MetricDraftRequest req,
            List<Map<String, Object>> indexes, ValueSample sample) {
        String driver = Py.truthy(contract.get("driver")) ? (String) contract.get("driver") : "postgresql";
        String anchor = sample.latest != null && !sample.latest.isEmpty()
                ? sample.latest.substring(0, Math.min(10, sample.latest.length())) : Times.day(LocalDate.now());
        String domain = Py.truthy(contract.get("domain")) ? "[업무 영역]\n" + contract.get("domain") + "\n" : "";
        String system = DRAFT_SYSTEM.replace("{name}", Py.text(contract.get("name"))).replace("{driver}", driver)
                .replace("{domain}", domain).replace("{anchor}", anchor).replace("{days}", String.valueOf(DRAFT_DAYS));

        String note = ContractService.firstText(table.get("purpose"), table.get("description"));
        List<String> lines = new ArrayList<>();
        lines.add("[테이블]\n[" + table.get("name") + "] " + Py.squeeze(note));
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

        List<Map<String, Object>> same = new ArrayList<>();
        List<Map<String, Object>> others = new ArrayList<>();
        for (Map<String, Object> m : Dyn.maps(contract.get("metrics"))) {
            (Dyn.strings(m.get("tables")).contains((String) table.get("name")) ? same : others).add(m);
        }
        List<Map<String, Object>> examples = !same.isEmpty() ? same : others.subList(0, Math.min(2, others.size()));
        if (!examples.isEmpty()) {
            lines.add(!same.isEmpty() ? "\n[같은 테이블 지표]" : "\n[다른 지표 예시] (같은 테이블 지표는 없습니다)");
            for (Map<String, Object> m : examples) {
                String desc = Py.squeeze(m.get("description"));
                lines.add("- " + m.get("name") + ": " + (desc.isEmpty() ? "(설명 없음)" : desc) + "\n  SQL:\n" + m.get("sql"));
            }
        }

        lines.add("\n[값 샘플] " + sample.note);
        if (!sample.rows.isEmpty()) {
            lines.add(String.join(" | ", sample.columns));
            for (List<String> r : sample.rows) {
                lines.add(String.join(" | ", r));
            }
        }
        if (sample.latest != null && !sample.latest.isEmpty()) {
            lines.add("가장 최근 시각: " + sample.latest);
        }

        String target = req.missing().column().isEmpty() ? req.missing().table()
                : req.missing().table() + "." + req.missing().column();
        lines.add("\n[빠진 계산]\n" + req.missing().name() + " (" + target + ")");
        lines.add("\n[질문]\n" + req.question());
        return new String[] {system, String.join("\n", lines)};
    }

    static String columnLine(Map<String, Object> c, String driver) {
        String note = Py.squeeze(c.get("description"));
        List<String> marks = new ArrayList<>();
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

    // ── 값 샘플 (인덱스를 타는 조회만) ─────────────────────────────────────

    /** → [인덱스 목록, 값 샘플]. 대상 DB 를 읽지 못해도 초안은 쓴다 — 샘플이 빈 채로 note 에 이유를 남긴다. */
    private Object[] collect(Map<String, Object> row, Map<String, Object> table, String column) {
        Set<String> names = new HashSet<>();
        Map<String, String> kinds = new LinkedHashMap<>();
        for (Map<String, Object> c : Dyn.maps(table.get("columns"))) {
            names.add((String) c.get("name"));
            kinds.put((String) c.get("name"), Compiler.temporalKind(Dyn.text(c, "type"), (String) row.get("driver")));
        }
        try (TargetSession cur = targetDb.readOnly(row, SAMPLE_TIMEOUT_S)) {
            List<Map<String, Object>> indexes = Catalog.readIndexes(cur, row, table, names);
            if (!names.contains(column)) {
                return new Object[] {indexes, ValueSample.note("계산할 컬럼을 몰라 값 샘플을 건너뛰었습니다")};
            }
            Sampler sampler = new Sampler(cur, new Dialect((String) row.get("driver")), table, kinds, column);
            try {
                return new Object[] {indexes, sampler.run(indexes)};
            } catch (FullScan error) {
                ValueSample sample = ValueSample.note(error.getMessage());
                sample.queries = sampler.queries;
                return new Object[] {indexes, sample};
            }
        } catch (ApiException error) {
            return new Object[] {new ArrayList<>(), ValueSample.note("대상 DB 를 읽지 못해 값 샘플을 건너뛰었습니다: " + error.detail())};
        } catch (SQLException | RuntimeException error) {
            return new Object[] {new ArrayList<>(), ValueSample.note("값 샘플 조회가 실패해 건너뛰었습니다: " + Py.firstLine(error))};
        }
    }

    /** 실행 계획이 큰 테이블을 전체 훑기로 읽는다 — 그 조회는 실행하지 않는다 */
    static final class FullScan extends RuntimeException {
        FullScan(String message) {
            super(message);
        }
    }

    /** 대상 컬럼의 값을 인덱스 순서로 몇 행만 읽는다. 실행한 조회는 queries 에 남는다. */
    static final class Sampler {
        final TargetSession cur;
        final Dialect d;
        final Map<String, Object> table;
        final Map<String, String> kinds;
        final String column;
        final List<String> queries = new ArrayList<>();
        final Map<String, Integer> offset = new LinkedHashMap<>();

        Sampler(TargetSession cur, Dialect dialect, Map<String, Object> table, Map<String, String> kinds, String column) {
            this.cur = cur;
            this.d = dialect;
            this.table = table;
            this.kinds = kinds;
            this.column = column;
            for (Map<String, Object> c : Dyn.maps(table.get("columns"))) {
                offset.put((String) c.get("name"), Dyn.integer(c, "utc_offset"));
            }
        }

        boolean timeKind(String col) {
            return kinds.get(col) != null;
        }

        ValueSample run(List<Map<String, Object>> indexes) throws SQLException {
            // 1) 묶음과 시각이 한 인덱스에 — 묶음 하나의 값을 시각 순으로 본다 (예: 차량 하나의 날짜별 첫 값 · 마지막 값)
            //    (묶음, 시각) 이면 묶음 = 값 · 시각 범위로, (시각, 묶음) 이면 시각 범위 안에서 묶음을 인덱스 칸으로 거른다
            for (Map<String, Object> ix : indexes) {
                List<String> cols = Dyn.strings(ix.get("columns"));
                if (cols.size() >= 2 && !cols.subList(0, 2).contains(column)) {
                    if (!timeKind(cols.get(0)) && timeKind(cols.get(1))) {
                        return series(ix, cols.get(0), cols.get(1), indexes);
                    }
                    if (timeKind(cols.get(0)) && !timeKind(cols.get(1))) {
                        return series(ix, cols.get(1), cols.get(0), indexes);
                    }
                }
            }
            // 2) 시각 인덱스와 묶음 인덱스가 따로 — 시각 범위로 읽고 묶음으로 거른다
            Map<String, Object> timeIx = null;
            String group = null;
            for (Map<String, Object> ix : indexes) {
                String head = Dyn.strings(ix.get("columns")).get(0);
                if (timeKind(head) && timeIx == null) {
                    timeIx = ix;
                } else if (!timeKind(head) && !head.equals(column) && group == null) {
                    group = head;
                }
            }
            if (timeIx != null && group != null) {
                return series(timeIx, group, Dyn.strings(timeIx.get("columns")).get(0), indexes);
            }
            // 3) 시각이 앞 칸인 인덱스 — 최근 행
            if (timeIx != null) {
                return recent(timeIx, Dyn.strings(timeIx.get("columns")).get(0), indexes);
            }
            // 4) 그 밖의 인덱스 — 앞 칸 큰 값부터 (일련번호 키면 최근 행이다)
            if (!indexes.isEmpty()) {
                return recent(indexes.get(0), Dyn.strings(indexes.get(0).get("columns")).get(0), indexes);
            }
            return ValueSample.note(table.get("name") + " 에 B-tree 인덱스가 없어 값 샘플을 건너뛰었습니다 (전체를 훑게 됩니다)");
        }

        ValueSample series(Map<String, Object> ix, String group, String time, List<Map<String, Object>> indexes) throws SQLException {
            String t = d.table((String) table.get("name"), Dyn.text(table, "schema"));
            boolean byTime = false;
            for (Map<String, Object> other : indexes) {
                if (Dyn.strings(other.get("columns")).get(0).equals(time)) {
                    byTime = true;
                }
            }
            List<List<Object>> picked;
            if (byTime) {
                // 최근에 기록된 묶음 몇 개 — 시각 인덱스를 거꾸로 몇 행
                picked = fetch("SELECT " + d.q(group) + " FROM " + t + " WHERE " + d.q(time) + " IS NOT NULL ORDER BY "
                        + d.q(time) + " DESC " + d.limit(CANDIDATE_ROWS));
            } else {
                picked = fetch("SELECT " + d.q(group) + " FROM " + t + " WHERE " + d.q(group) + " IS NOT NULL ORDER BY "
                        + d.q(group) + " DESC " + d.limit(1));
            }
            List<Object> keys = new ArrayList<>();
            for (List<Object> r : picked) {
                if (r.get(0) != null && !keys.contains(r.get(0)) && keys.size() < CANDIDATES) {
                    keys.add(r.get(0));
                }
            }

            String select = "SELECT " + d.q(time) + ", " + d.q(group) + ", " + d.q(column) + " FROM " + t + " WHERE "
                    + d.q(group) + " = ?";
            String ranged = select + " AND " + d.q(time) + " >= ? AND " + d.q(time) + " < ? ORDER BY " + d.q(time);
            int hours = "datetime".equals(kinds.get(time)) ? offset.getOrDefault(time, 0) : 0;
            Object[] chosen = null;
            for (Object key : keys) {
                List<List<Object>> recent = fetch(select + " AND " + d.q(time) + " IS NOT NULL ORDER BY " + d.q(time) + " DESC "
                        + d.limit(RECENT_ROWS), key);
                if (recent.isEmpty()) {
                    continue;
                }
                List<Object[]> days = localDays(recent.get(0).get(0), hours);
                Object[] oldest = days.get(days.size() - 1);
                List<List<Object>> firstOld = fetch(ranged + " ASC " + d.limit(1), key, oldest[1], oldest[2]);
                if (chosen == null) {
                    chosen = new Object[] {key, recent, days, firstOld};
                }
                // 가장 오래된 날에도 기록이 있고 그 뒤로 값이 바뀐 묶음이어야 계산 방식이 보인다
                // (방금 기록을 시작했거나 값이 그대로인 묶음은 건너뛴다)
                if (!firstOld.isEmpty() && !Objects.equals(firstOld.get(0).get(2), recent.get(0).get(2))) {
                    chosen = new Object[] {key, recent, days, firstOld};
                    break;
                }
            }
            if (chosen == null) {
                ValueSample out = ValueSample.note("테이블이 비어 값 샘플이 없습니다");
                out.index = (String) ix.get("name");
                out.queries = queries;
                return out;
            }
            Object key = chosen[0];
            @SuppressWarnings("unchecked")
            List<List<Object>> recent = (List<List<Object>>) chosen[1];
            @SuppressWarnings("unchecked")
            List<Object[]> days = (List<Object[]>) chosen[2];
            @SuppressWarnings("unchecked")
            List<List<Object>> firstOld = (List<List<Object>>) chosen[3];
            Object latest = recent.get(0).get(0);
            LocalDateTime local = asDatetime(latest).plusHours(hours);
            List<Object[]> labeled = new ArrayList<>();
            for (List<Object> r : recent) {
                labeled.add(new Object[] {"최근", r});
            }
            String oldestDay = (String) days.get(days.size() - 1)[0];
            for (List<Object> r : firstOld) {
                labeled.add(new Object[] {oldestDay + " 첫 값", r});
            }

            // 현지 날짜마다 첫 값 · 마지막 값 — (묶음 = 값, 시각 범위) 라 인덱스 범위 조회다
            for (Object[] day : days) {
                if (!day[0].equals(oldestDay)) {
                    for (List<Object> r : fetch(ranged + " ASC " + d.limit(1), key, day[1], day[2])) {
                        labeled.add(new Object[] {day[0] + " 첫 값", r});
                    }
                }
                for (List<Object> r : fetch(ranged + " DESC " + d.limit(1), key, day[1], day[2])) {
                    labeled.add(new Object[] {day[0] + " 마지막 값", r});
                }
            }

            labeled.sort(Comparator.comparing(pair -> sortKey(((List<?>) pair[1]).get(0))));
            List<List<String>> rows = new ArrayList<>();
            Set<List<String>> seen = new HashSet<>();
            for (Object[] pair : labeled) {
                @SuppressWarnings("unchecked")
                List<String> printed = shown((List<Object>) pair[1]);
                if (!seen.add(printed)) {
                    continue;
                }
                List<String> line = new ArrayList<>();
                line.add((String) pair[0]);
                line.addAll(printed);
                rows.add(line);
            }
            String where = group + " = " + show(key);
            String tz = hours != 0 ? ", 현지 = UTC " + Py.signed(hours) + "시간" : "";
            ValueSample out = new ValueSample();
            out.index = (String) ix.get("name");
            out.queries = queries;
            out.columns = new ArrayList<>(List.of("구분", time, group, column));
            out.rows = rows;
            out.latest = show(local);
            out.note = "인덱스 " + ix.get("name") + " (" + String.join(", ", Dyn.strings(ix.get("columns"))) + ") 로 " + where
                    + " 의 최근 " + RECENT_ROWS + "행과 최근 " + SAMPLE_DAYS + "일의 날짜별 첫 값 · 마지막 값을 읽었습니다" + tz;
            return out;
        }

        ValueSample recent(Map<String, Object> ix, String order, List<Map<String, Object>> indexes) throws SQLException {
            String t = d.table((String) table.get("name"), Dyn.text(table, "schema"));
            String time = timeKind(order) ? order : null;
            if (time == null) {
                for (String kind : List.of("datetime", "date")) {
                    for (Map<String, Object> c : Dyn.maps(table.get("columns"))) {
                        if (time == null && kind.equals(kinds.get((String) c.get("name")))) {
                            time = (String) c.get("name");
                        }
                    }
                }
            }
            // 묶음 후보: 다른 인덱스의 앞 칸 (대개 장비 · 차량 키)
            List<String> groups = new ArrayList<>();
            for (Map<String, Object> other : indexes) {
                String head = Dyn.strings(other.get("columns")).get(0);
                if (!timeKind(head) && !head.equals(column) && !head.equals(order) && !groups.contains(head)) {
                    groups.add(head);
                }
            }
            List<String> shownColumns = new ArrayList<>();
            List<String> wanted = new ArrayList<>();
            wanted.add(order);
            wanted.add(time);
            wanted.addAll(groups.subList(0, Math.min(3, groups.size())));
            wanted.add(column);
            for (String c : wanted) {
                if (c != null && !shownColumns.contains(c)) {
                    shownColumns.add(c);
                }
            }
            List<String> quoted = new ArrayList<>();
            for (String c : shownColumns) {
                quoted.add(d.q(c));
            }
            List<List<Object>> fetched = fetch("SELECT " + String.join(", ", quoted) + " FROM " + t + " WHERE " + d.q(order)
                    + " IS NOT NULL ORDER BY " + d.q(order) + " DESC " + d.limit(SCAN_ROWS));
            if (fetched.isEmpty()) {
                ValueSample out = ValueSample.note("테이블이 비어 값 샘플이 없습니다");
                out.index = (String) ix.get("name");
                out.queries = queries;
                return out;
            }

            // 여러 행이 같은 값을 갖는 첫 묶음 후보로, 가장 많이 나온 값 하나의 행만 남긴다
            List<List<Object>> rows = fetched;
            String where = "";
            for (String g : groups.subList(0, Math.min(3, groups.size()))) {
                int at = shownColumns.indexOf(g);
                Map<Object, Integer> counts = new LinkedHashMap<>();
                for (List<Object> r : fetched) {
                    counts.merge(r.get(at), 1, Integer::sum);
                }
                if (counts.size() < fetched.size()) {
                    Object key = null;
                    int best = -1;
                    for (Map.Entry<Object, Integer> e : counts.entrySet()) {
                        if (e.getValue() > best) {
                            best = e.getValue();
                            key = e.getKey();
                        }
                    }
                    rows = new ArrayList<>();
                    for (List<Object> r : fetched) {
                        if (Objects.equals(r.get(at), key)) {
                            rows.add(r);
                        }
                    }
                    where = " 중 " + g + " = " + show(key);
                    break;
                }
            }
            if (time != null) {
                int at = shownColumns.indexOf(time);
                rows = new ArrayList<>(rows);
                rows.sort(Comparator.comparing(r -> sortKey(r.get(at))));
            }
            rows = rows.subList(Math.max(0, rows.size() - SHOW_ROWS), rows.size());
            String latest = null;
            if (time != null) {
                int hours = "datetime".equals(kinds.get(time)) ? offset.getOrDefault(time, 0) : 0;
                latest = show(asDatetime(rows.get(rows.size() - 1).get(shownColumns.indexOf(time))).plusHours(hours));
            }
            List<List<String>> printed = new ArrayList<>();
            for (List<Object> r : rows) {
                printed.add(shown(r));
            }
            ValueSample out = new ValueSample();
            out.index = (String) ix.get("name");
            out.queries = queries;
            out.columns = shownColumns;
            out.rows = printed;
            out.latest = latest;
            out.note = "인덱스 " + ix.get("name") + " (" + String.join(", ", Dyn.strings(ix.get("columns"))) + ") 순서로 최근 "
                    + SCAN_ROWS + "행" + where + "을 읽었습니다";
            return out;
        }

        List<List<Object>> fetch(String sql, Object... params) throws SQLException {
            String text = sql;
            if (params.length > 0) {
                text += "  -- " + String.join(", ", shown(List.of(params)));
            }
            queries.add(text);
            checkPlan(sql, params);
            return cur.rows(sql, params);
        }

        void checkPlan(String sql, Object[] params) throws SQLException {
            List<String> scanned = Plans.fullScans(cur, d, sql, params);
            if (!scanned.isEmpty()) {
                throw new FullScan("실행 계획이 " + String.join(", ", scanned) + " 전체를 훑어 값 샘플을 건너뛰었습니다");
            }
        }
    }

    /** latest 의 현지 날짜부터 SAMPLE_DAYS 일 → [(현지 날짜, 시작, 끝)] (시작 · 끝은 저장된 시각 기준, 최근 날이 먼저) */
    static List<Object[]> localDays(Object latest, int hours) {
        LocalDateTime local = asDatetime(latest).plusHours(hours);
        List<Object[]> days = new ArrayList<>();
        for (int i = 0; i < SAMPLE_DAYS; i++) {
            LocalDate day = local.toLocalDate().minusDays(i);
            LocalDateTime start = day.atStartOfDay().minusHours(hours);
            Object startValue = start;
            Object endValue = start.plusDays(1);
            if (latest instanceof OffsetDateTime aware) {
                startValue = start.atOffset(aware.getOffset());
                endValue = start.plusDays(1).atOffset(aware.getOffset());
            }
            days.add(new Object[] {Times.day(day), startValue, endValue});
        }
        return days;
    }

    static LocalDateTime asDatetime(Object value) {
        if (value instanceof LocalDateTime t) {
            return t;
        }
        if (value instanceof OffsetDateTime t) {
            return t.toLocalDateTime();
        }
        if (value instanceof LocalDate t) {
            return t.atStartOfDay();
        }
        return LocalDateTime.MIN;
    }

    /** 시각 정렬용. 한 컬럼의 값끼리만 비교하므로 시간대는 떼고, 비어 있으면 맨 앞이다. */
    static LocalDateTime sortKey(Object value) {
        return asDatetime(value);
    }

    static List<String> shown(List<Object> values) {
        List<String> out = new ArrayList<>();
        for (Object v : values) {
            out.add(show(v));
        }
        return out;
    }

    static String show(Object value) {
        if (value == null) {
            return "NULL";
        }
        if (value instanceof LocalDateTime t) {
            return Times.seconds(t);
        }
        if (value instanceof OffsetDateTime t) {
            return Times.seconds(t);
        }
        if (value instanceof LocalDate t) {
            return Times.day(t);
        }
        String text = value instanceof Number n && !(value instanceof java.math.BigDecimal) ? Py.repr(n) : String.valueOf(value);
        return text.length() <= 60 ? text : text.substring(0, 57) + "...";
    }
}
