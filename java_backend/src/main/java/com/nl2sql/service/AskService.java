package com.nl2sql.service;

import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.springframework.stereotype.Service;

import com.nl2sql.common.Dyn;
import com.nl2sql.common.Json;
import com.nl2sql.common.Py;
import com.nl2sql.compiler.CalcColumnError;
import com.nl2sql.compiler.Compiler;
import com.nl2sql.llm.Llm;
import com.nl2sql.llm.LlmCalls;
import com.nl2sql.llm.Step;

/**
 * 질문 하나를 처리한다: 테이블 선택(지표 필요 판단) → AST 생성(LLM) → 컴파일(조합·검증) (ask.py).
 *
 * 지표가 필요한 질문인데 맞는 지표가 없으면 SQL 을 만들지 않고 '지표 등록이 필요합니다' 로 끝난다.
 * 지표를 쓰면 고른 지표(질문이 묻는 계산마다 하나)의 AST 를 모두 LLM 에 주고,
 * 질문에 맞게 조건만 바꾸거나 지표들의 계산 식을 한 AST 로 합치게 한다.
 */
@Service
public class AskService {

    public static final int MAX_ATTEMPTS = 5;

    static final String AST_SYSTEM = """
            당신은 질문을 '{name}' 데이터베이스({driver})의 조회 명세(AST, JSON)로 바꾸는 역할입니다.
            {domain}[오늘]
            {today}. "오늘", "어제", "최근 7일", "지난달" 같은 표현은 이 날짜를 기준으로 계산해 'YYYY-MM-DD' 또는 'YYYY-MM-DD HH:MM:SS' 로 적습니다.
            기간 조건에 CURRENT_DATE 를 쓰지 않습니다.

            [AST 모양]
            {
              "with": [{"name": "이름", "query": 하위 AST}],   (같은 하위 쿼리를 두 곳 이상에서 쓸 때만. 맨 바깥에만 둡니다)
              "from": "테이블" 또는 {"table": "테이블", "as": "별칭"} 또는 {"query": 하위 AST, "as": "별칭"} 또는 {"tree": 계층, "as": "별칭"},
              "joins": [{"table": "테이블" 또는 "query": 하위 AST, "as": "별칭", "type": "inner|left", "on": [["a.컬럼", "b.컬럼"]]}],   (필요할 때만)
              "select": [{"expr": 식, "as": "결과 이름"}],   모든 컬럼은 {"expr": {"col": "*"}}
              "distinct": true,   (중복을 뺄 때만)
              "where": [조건], "group_by": [식], "having": [조건],
              "order_by": [{"expr": 식, "dir": "asc|desc"}], "limit": 숫자
            }
            식: {"col": "컬럼"} | {"num": 1} | {"str": "문자"} | {"fn": "함수", "args": [식, ...]}
              집계: SUM AVG MIN MAX COUNT(인자 없으면 *) COUNT_DISTINCT
              계산: ADD SUB MUL DIV(0 으로 나누면 NULL)
              함수: ABS ROUND COALESCE NULLIF GREATEST LEAST
              날짜: DATE(날짜만), {"fn": "DATE_TRUNC", "unit": "hour|day|week|month|year", "args": [식]},
                    ADD_SECONDS · ADD_MINUTES · ADD_HOURS · ADD_DAYS [식, {"num": n}], NOW, CURRENT_DATE
              순위: {"fn": "ROW_NUMBER", "partition_by": [식], "order_by": [{"expr": 식, "dir": "desc"}]}
              분기: {"fn": "CASE", "when": [{"if": [조건], "then": 식}], "else": 식}
            조건: {"expr": 식, "op": "= != > >= < <= in not_in between like not_like is_null is_not_null", "value": 값 또는 식}
              in · not_in 의 value 는 {"query": 하위 AST} (select 하나) 도 됩니다
            계층: {"table": "테이블", "key": "id 컬럼", "parent": "부모 id 컬럼", "start": [조건], "direction": "down|up"}
              start 조건에 맞는 행에서 시작해 자식 쪽(down) 또는 부모 쪽(up)으로 따라간 행 전부입니다. 테이블의 컬럼에 depth(시작 = 0)가 더해집니다.
                  목록은 모두 AND. OR 은 {"or": [조건, ...]}, 그 안의 AND 묶음은 {"and": [조건, ...]}

            [규칙]
            - 아래 [테이블] 에 없는 테이블·컬럼은 쓰지 않습니다. 여러 테이블을 쓰면 컬럼을 '테이블.컬럼' 으로 적습니다.
            - 집계 안에 집계는 쓸 수 없습니다. 두 단계 집계(예: 장비·일별로 먼저 구한 뒤 일별 합계)는 from 에 하위 쿼리를 둡니다.
            - 상위·하위 관계(그룹 계층, 조직도, 메뉴 트리)를 따라가야 하면 tree 를 씁니다. 시작이 다른 테이블의 값이면
              (예: 사용자의 그룹) start 에 {"expr": {"col": "id"}, "op": "in", "value": {"query": 하위 AST}} 로 적습니다.
              타입이 다른 컬럼끼리(정수 id 와 문자 parent_id) 비교해도 컴파일러가 맞춰 주므로 변환하지 않습니다.
            - 하위 쿼리 바깥에서는 하위 쿼리 select 의 "as" 이름을 컬럼으로 씁니다.
            - 순위로 거르려면(예: 단위별 마지막 건) 하위 쿼리에서 ROW_NUMBER 를 구하고 바깥 where 에서 거릅니다.
            - with 에 정의한 이름은 from · joins 에서 테이블처럼 씁니다 ("from": "daily"). 컬럼은 그 하위 쿼리 select 의 "as" 이름입니다.
            - 기간 합계로 고른 상위 N개 묶음의 상세(예: 50일 합계 상위 5대 차량의 일별 값)는, 상세 집계(예: 차량·일별)를 with 에
              한 번 정의하고, from 은 그 이름, joins 에는 그 이름을 묶음별로 합쳐 상위 N개를 고르는 하위 쿼리(묶음별 합계, order_by, limit)를
              {"query": …, "as": "top", "type": "inner"} 로 두어 묶음 컬럼으로 조인합니다. 원본 테이블을 두 번 읽지 않습니다.
              상세 쪽을 order_by 와 limit 으로 자르면 묶음이 아니라 행이 잘립니다.
            - 집계하지 않은 select 항목은 group_by 에 같은 식으로 넣습니다.
            - 컬럼 뒤의 [UTC 보정] 은 DB 에 UTC 로 저장됐지만 자동으로 현지 시각으로 바뀌는 컬럼입니다. 날짜·시각은 현지 기준 그대로 적고, ADD_HOURS 로 직접 바꾸지 마십시오.
            - 시각을 포함한 컬럼의 기간은 시작은 >=, 끝은 < 다음 날로 적습니다.
            - 질문만으로 조회를 정할 수 없으면 {"clarify": "사용자에게 물을 한 문장"} 으로 답합니다.
            - 설명 없이 JSON 객체 하나로만 답합니다: {"ast": AST, "reason": "설명"}""";

    static final String METRIC_GUIDE = """
            [지표] {name} — {description}
            등록된 SQL:
            {sql}
            등록된 AST:
            {ast}
            이 지표의 AST 를 바탕으로 질문에 맞게 기간·조건·묶음·정렬·개수만 바꾸십시오. 계산 부분(select 의 식)은 그대로 둡니다.""";

    static final String METRICS_RULE = """
            [지표 여러 개]
            위 지표들의 계산 식을 한 AST 에 합칩니다.
            - 묶음(group_by)과 기간이 같으면 각 지표의 select 계산 식을 그대로 옮겨 나란히 둡니다.
            - 질문이 지표끼리의 계산(차이, 합, 비율)을 물으면 각 지표의 계산 식을 그대로 SUB, ADD, DIV 의 인자로 씁니다.
            - 지표에 없는 새 계산([계산 필요] 컬럼의 집계)은 만들지 않습니다. 결과 이름("as")은 그 지표가 뜻하는 값으로 붙입니다.""";

    private static final DateTimeFormatter DAY = DateTimeFormatter.ofPattern("yyyy-MM-dd");
    private static final DateTimeFormatter CLOCK = DateTimeFormatter.ofPattern("HH:mm");
    private static final String WEEKDAYS = "월화수목금토일";

    public static String columnLine(Map<String, Object> c, String driver) {
        String note = Py.squeeze(c.get("description"));
        if (Py.truthy(c.get("utc_offset")) && "datetime".equals(Compiler.temporalKind(Dyn.text(c, "type"), driver))) {
            note = (note + " [UTC 보정]").strip();
        }
        return "  - " + c.get("name") + " (" + Dyn.text(c, "type") + ")" + (note.isEmpty() ? "" : ": " + note);
    }

    /** → [시스템 프롬프트, 사용자 메시지 앞부분] */
    public static String[] astPrompt(Map<String, Object> contract, List<String> tables, List<Map<String, Object>> metrics,
            LocalDateTime now) {
        String driver = Py.truthy(contract.get("driver")) ? (String) contract.get("driver") : "postgresql";
        String today = now.format(DAY) + " (" + WEEKDAYS.charAt(now.getDayOfWeek().getValue() - 1) + "요일) 현재 시각 "
                + now.format(CLOCK);
        String domain = Py.truthy(contract.get("domain")) ? "[업무 영역]\n" + contract.get("domain") + "\n" : "";
        String system = AST_SYSTEM.replace("{name}", Py.text(contract.get("name"))).replace("{driver}", driver)
                .replace("{domain}", domain).replace("{today}", today);
        List<String> blocks = new ArrayList<>();
        for (Map<String, Object> t : Dyn.maps(contract.get("tables"))) {
            if (tables.contains((String) t.get("name"))) {
                String note = ContractService.firstText(t.get("purpose"), t.get("description"));
                List<String> lines = new ArrayList<>();
                for (Map<String, Object> c : Dyn.maps(t.get("columns"))) {
                    lines.add(columnLine(c, driver));
                }
                blocks.add("[" + t.get("name") + "] " + Py.squeeze(note) + "\n" + String.join("\n", lines));
            }
        }
        for (Map<String, Object> r : Dyn.maps(contract.get("relations"))) {
            if (tables.contains((String) r.get("left_table")) && tables.contains((String) r.get("right_table"))) {
                blocks.add("[관계] " + r.get("left_table") + "." + r.get("left_column") + " = " + r.get("right_table") + "."
                        + r.get("right_column"));
            }
        }
        StringBuilder user = new StringBuilder("[테이블]\n" + String.join("\n\n", blocks));
        for (Map<String, Object> metric : metrics) {
            String description = Py.squeeze(metric.get("description"));
            user.append("\n\n").append(METRIC_GUIDE
                    .replace("{name}", String.valueOf(metric.get("name")))
                    .replace("{description}", description.isEmpty() ? "(설명 없음)" : description)
                    .replace("{sql}", String.valueOf(metric.get("sql")))
                    .replace("{ast}", Json.dumps(metric.get("ast"))));
        }
        if (metrics.size() > 1) {
            user.append("\n\n").append(METRICS_RULE);
        }
        return new String[] {system, user.toString()};
    }

    /** 질문에 질문한 사용자를 붙인다. '나', '내 권한', '로그인한 사용자' 를 LLM 이 이 아이디로 풀게 한다. */
    public static String withUser(String question, String userId) {
        String user = Py.squeeze(userId);
        if (user.isEmpty()) {
            return question;
        }
        return "[현재 사용자]\n로그인한 사용자 아이디: " + user + "\n"
                + "질문의 '나', '내', '현재 로그인한 사용자' 는 이 사용자입니다. 조건에 이 아이디를 그대로 씁니다.\n\n"
                + "[질문]\n" + question;
    }

    public Map<String, Object> runQuestion(Map<String, Object> contract, String question, Llm model, String userId) {
        return runQuestion(contract, question, model, null, userId);
    }

    /**
     * 질문 하나를 처리한다. 상태는 이 호출 안에서만 산다.
     *
     * 돌려주는 것: {sql, error, attempts, ast, clarification, needs_metric, missing, tables, metrics, steps, total_tokens}
     */
    public Map<String, Object> runQuestion(Map<String, Object> contract, String question, Llm model, LocalDateTime now,
            String userId) {
        Map<String, Object> outcome = Dyn.of(
                "sql", null, "error", null, "attempts", 0, "ast", null, "clarification", null, "needs_metric", false,
                "missing", new ArrayList<>(), "tables", new ArrayList<>(), "metrics", new ArrayList<>(),
                "steps", new ArrayList<>(), "total_tokens", 0);
        question = withUser(question, userId);
        Selector.Selection selection;
        try {
            selection = Selector.selectTables(model, contract, question);
        } catch (IllegalArgumentException error) {
            outcome.put("error", error.getMessage());
            return outcome;
        }
        outcome.put("tables", selection.tables());
        outcome.put("metrics", new ArrayList<>(selection.metrics()));
        Dyn.items(outcome.get("steps")).add(stepMap(selection.step()));
        if (selection.clarification() != null) {
            outcome.put("clarification", selection.clarification());
            return withTokens(outcome);
        }
        if (selection.needsMetric() && selection.metrics().isEmpty()) {
            outcome.put("needs_metric", true);
            outcome.put("missing", selection.missing());
            outcome.put("error", "지표 등록이 필요합니다 — " + (selection.reason().isEmpty()
                    ? "계산이 필요한 질문인데 맞는 지표가 없습니다" : selection.reason()));
            return withTokens(outcome);
        }

        List<Map<String, Object>> metrics = new ArrayList<>();
        for (String name : selection.metrics()) {
            for (Map<String, Object> m : Dyn.maps(contract.get("metrics"))) {
                if (name.equals(m.get("name"))) {
                    metrics.add(m);
                    break;
                }
            }
        }
        String[] prompt = astPrompt(contract, selection.tables(), metrics, now != null ? now : LocalDateTime.now());
        List<Llm.Message> messages = new ArrayList<>(List.of(Llm.Message.system(prompt[0]),
                Llm.Message.user(prompt[1] + "\n\n[질문]\n" + question)));
        // [계산 필요] 컬럼의 계산은 고른 지표의 식으로만 허용한다 — 지표 없이 LLM 이 만든 계산은 틀리기 쉽다
        Compiler compiler = new Compiler(contract, metrics);
        Step step = new Step("sql", model.modelName());
        for (int attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
            Llm.Reply reply = LlmCalls.chat(model, messages, step, attempt);
            outcome.put("attempts", attempt);
            Map<String, Object> parsed;
            String sql;
            try {
                parsed = LlmCalls.parseJson(reply.content());
                if (!Py.text(parsed.get("clarify")).strip().isEmpty() && !Py.truthy(parsed.get("ast"))) {
                    outcome.put("clarification", Py.text(parsed.get("clarify")).strip());
                    outcome.put("error", null);
                    step.reason = "모델이 되물음";
                    break;
                }
                sql = compiler.compile(parsed.get("ast"));
            } catch (CalcColumnError error) {
                if (!covered(metrics, error.column())) {
                    // 고른 지표 어디에도 이 컬럼의 계산이 없다 — 다시 시켜도 지표 없이 계산할 뿐이라 지표 등록으로 넘긴다
                    needsMetricFor(outcome, contract, error);
                    step.reason = attempt + "번째 시도: " + error.getMessage();
                    break;
                }
                retry(outcome, step, messages, reply, attempt, error.getMessage());
                continue;
            } catch (IllegalArgumentException | ClassCastException | NullPointerException error) {
                retry(outcome, step, messages, reply, attempt, error.getMessage());
                continue;
            }
            outcome.put("sql", sql);
            outcome.put("ast", parsed.get("ast"));
            outcome.put("error", null);
            step.selected = new ArrayList<>(List.of(Json.dumps(parsed.get("ast"))));
            step.reason = Py.truthy(parsed.get("reason")) ? Py.text(parsed.get("reason")) : attempt + "번째 시도에서 컴파일 성공";
            break;
        }
        Dyn.items(outcome.get("steps")).add(stepMap(step));
        return withTokens(outcome);
    }

    private static void retry(Map<String, Object> outcome, Step step, List<Llm.Message> messages, Llm.Reply reply, int attempt,
            String error) {
        outcome.put("error", error);
        step.reason = attempt + "번째 시도 실패: " + error;
        messages.add(Llm.Message.assistant(reply.content()));
        messages.add(Llm.Message.user("오류: " + error + "\n고쳐서 JSON 으로 다시 답하십시오."));
    }

    /** 고른 지표 중 이 컬럼을 쓰는 것이 있나 */
    private static boolean covered(List<Map<String, Object>> metrics, String column) {
        for (Map<String, Object> m : metrics) {
            Set<String> used = new HashSet<>();
            columnsIn(m.get("ast"), used);
            if (used.contains(column)) {
                return true;
            }
        }
        return false;
    }

    /** AST 가 쓰는 컬럼 이름 ('테이블.' 은 뺀다) */
    static void columnsIn(Object node, Set<String> out) {
        if (node instanceof List<?> list) {
            for (Object x : list) {
                columnsIn(x, out);
            }
        } else if (node instanceof Map<?, ?> raw) {
            Map<String, Object> m = Dyn.map(raw);
            if (m.get("col") instanceof String col) {
                String[] parts = col.split("\\.", -1);
                out.add(parts[parts.length - 1]);
            }
            for (Object value : m.values()) {
                columnsIn(value, out);
            }
        }
    }

    /** 지표 없는 [계산 필요] 컬럼 계산 → '지표 등록이 필요합니다' 로 끝낸다 (테이블 선택 단계가 놓친 경우) */
    private static void needsMetricFor(Map<String, Object> outcome, Map<String, Object> contract, CalcColumnError error) {
        String name = error.column();
        for (Map<String, Object> t : Dyn.maps(contract.get("tables"))) {
            if (error.table().equals(t.get("name"))) {
                for (Map<String, Object> c : Dyn.maps(t.get("columns"))) {
                    if (error.column().equals(c.get("name")) && !Py.text(c.get("description")).strip().isEmpty()) {
                        name = Py.squeeze(c.get("description"));
                    }
                }
            }
        }
        outcome.put("needs_metric", true);
        outcome.put("missing", new ArrayList<>(List.of(Dyn.of("name", name, "table", error.table(), "column", error.column()))));
        outcome.put("error", "지표 등록이 필요합니다 — " + error.table() + "." + error.column() + "(" + name
                + ") 은 계산이 필요한 컬럼인데 이 계산의 지표가 없습니다");
    }

    private static Map<String, Object> withTokens(Map<String, Object> outcome) {
        int total = 0;
        for (Map<String, Object> s : Dyn.maps(outcome.get("steps"))) {
            total += Dyn.integer(s, "prompt_tokens") + Dyn.integer(s, "completion_tokens");
        }
        outcome.put("total_tokens", total);
        return outcome;
    }

    /** ask_trace 한 행 */
    public static Map<String, Object> stepMap(Step step) {
        return Dyn.of("stage", step.stage, "candidates", step.candidates, "selected", step.selected, "reason", step.reason,
                "prompt_tokens", step.promptTokens, "completion_tokens", step.completionTokens,
                "elapsed_ms", step.elapsedMs, "model", step.model);
    }
}
