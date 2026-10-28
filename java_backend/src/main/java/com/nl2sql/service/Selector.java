package com.nl2sql.service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import com.nl2sql.common.Dyn;
import com.nl2sql.common.Py;
import com.nl2sql.llm.Llm;
import com.nl2sql.llm.LlmCalls;
import com.nl2sql.llm.Step;

/**
 * 질문에 쓸 테이블을 고르고, 지표가 필요한 질문인지 판단한다 (selector.py).
 *
 * 테이블마다 컬럼(계산 필요 표시 포함)과 그 테이블을 쓰는 지표의 설명을 함께 보낸다.
 * 계약서(Map)만 보고 돈다 — DB 없이도 돌릴 수 있게.
 */
public final class Selector {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.selector");

    /** 고른 테이블 · 지표와 판단 */
    public record Selection(List<String> tables, List<String> metrics, boolean needsMetric, String reason, Step step,
            String clarification, List<Map<String, Object>> missing) {
    }

    static final String TABLE_SYSTEM = """
            당신은 업무 시스템 '{name}' 의 테이블 목록에서 질문에 답할 테이블을 고르고, 지표가 필요한 질문인지 판단하는 역할입니다.
            [업무 영역]
            {domain}

            [규칙]
            - 질문에 답하는 데 꼭 필요한 테이블만 고릅니다 (보통 하나, 조인이 필요할 때만 여럿). 목록의 테이블 이름을 그대로 적습니다.
            - 당신이 할 수 없는 행동을 제안하지 않습니다.
            - 컬럼 뒤의 [계산 필요] 는 값을 그대로 합치거나 평균내면 틀리는 컬럼입니다 (예: 계속 커지는 누적값).
              이 컬럼으로 합계·사용량·기간 증감·비율처럼 계산해야 답할 수 있는 질문은 지표가 필요한 질문입니다 (needs_metric: true).
              값을 그대로 보여 주기만 하면 (목록, 마지막 값 등) 지표가 필요하지 않습니다.
            - 지표가 필요한 질문이면 질문이 묻는 계산마다 그 테이블의 [지표] 중 맞는 지표를 골라 metrics 에 이름을 그대로 적습니다.
              계산이 여러 개면 (예: 무인 가동시간과 유인 가동시간) 지표도 모두 적습니다.
              계산 중 하나라도 맞는 지표가 없으면 metrics 는 빈 목록이고, reason 에 어떤 계산의 지표가 없는지 적습니다.
              그리고 missing 에 지표가 없는 계산마다 {"name": "계산 이름", "table": "테이블", "column": "계산할 [계산 필요] 컬럼"} 을 적습니다.
              name 은 기간·묶음·합계 같은 말을 뺀 계산 자체의 이름입니다 (예: '최근 7일 차량별 무인 절단 시간 합계' → '무인 절단 시간').
            - 질문이 여러 갈래로 읽혀 정할 수 없을 때만 clarify 에 사용자에게 물을 한 문장을 적습니다. 그 밖에는 빈 문자열입니다.
            - 설명 없이 JSON 객체 하나로만 답합니다:
              {"tables": ["테이블"], "needs_metric": false, "metrics": [], "missing": [], "reason": "판단 이유", "clarify": ""}""";

    private Selector() {
    }

    static String squash(Object text) {
        return Py.text(text).replaceAll("\\s+", "").toLowerCase();
    }

    /** 테이블 선택에 보내는 테이블 하나: 설명 · 컬럼(계산 필요 표시) · 지표 */
    static String tableBlock(Map<String, Object> t, List<Map<String, Object>> metrics) {
        String note = ContractService.firstText(t.get("purpose"), t.get("description"), "(설명 없음)");
        List<String> columns = new ArrayList<>();
        for (Map<String, Object> c : Dyn.maps(t.get("columns"))) {
            String text = Py.truthy(c.get("description")) ? c.get("name") + "(" + c.get("description") + ")" : (String) c.get("name");
            columns.add(text + (Dyn.bool(c, "needs_calc") ? " [계산 필요]" : ""));
        }
        List<String> lines = new ArrayList<>();
        lines.add("- " + t.get("name") + ": " + Py.squeeze(note));
        lines.add("  컬럼: " + String.join(", ", columns));
        for (Map<String, Object> m : metrics) {
            String desc = Py.squeeze(m.get("description"));
            if (desc.isEmpty()) {
                desc = "(설명 없음)";
            }
            String same = Py.truthy(m.get("synonyms")) ? " (같은 말: " + String.join(", ", Dyn.strings(m.get("synonyms"))) + ")" : "";
            lines.add("  [지표] " + m.get("name") + ": " + desc + same);
        }
        return String.join("\n", lines);
    }

    public static Selection selectTables(Llm model, Map<String, Object> contract, String question) {
        List<Map<String, Object>> tables = Dyn.maps(contract.get("tables"));
        List<Map<String, Object>> metrics = Dyn.maps(contract.get("metrics"));
        Map<String, List<Map<String, Object>>> byTable = new LinkedHashMap<>();
        for (Map<String, Object> m : metrics) {
            for (String name : Dyn.strings(m.get("tables"))) {
                byTable.computeIfAbsent(name, k -> new ArrayList<>()).add(m);
            }
        }
        List<String> candidates = new ArrayList<>();
        Map<String, String> names = new LinkedHashMap<>();
        Map<String, Map<String, String>> columns = new LinkedHashMap<>();
        List<String> blocks = new ArrayList<>();
        for (Map<String, Object> t : tables) {
            String name = (String) t.get("name");
            candidates.add(name);
            names.put(name.toLowerCase(), name);
            Map<String, String> cols = new LinkedHashMap<>();
            for (Map<String, Object> c : Dyn.maps(t.get("columns"))) {
                cols.put(((String) c.get("name")).toLowerCase(), (String) c.get("name"));
            }
            columns.put(name, cols);
            blocks.add(tableBlock(t, byTable.getOrDefault(name, List.of())));
        }
        Map<String, Map<String, Object>> metricNames = new LinkedHashMap<>();
        for (Map<String, Object> m : metrics) {
            metricNames.put(squash(m.get("name")), m);
        }
        Step step = new Step("table", candidates, model.modelName());

        String system = TABLE_SYSTEM.replace("{name}", Py.text(contract.get("name")))
                .replace("{domain}", Py.truthy(contract.get("domain")) ? String.valueOf(contract.get("domain")) : "(설명 없음)");
        String user = "[테이블 목록]\n" + String.join("\n", blocks) + "\n\n[질문]\n" + question;
        Map<String, Object> parsed = LlmCalls.askJson(model, system, user, step, p -> {
            List<String> picked = new ArrayList<>();
            for (Object n : Dyn.items(p.get("tables"))) {
                String real = names.get(Py.str(n).strip().toLowerCase());
                if (real == null) {
                    throw new IllegalArgumentException("'" + Py.str(n) + "' 는 [테이블 목록] 에 없는 테이블입니다");
                }
                if (!picked.contains(real)) {
                    picked.add(real);
                }
            }
            Object asked = p.get("metrics");
            List<Object> askedList;
            if (asked == null) {
                askedList = Py.truthy(p.get("metric")) ? new ArrayList<>(List.of(p.get("metric"))) : new ArrayList<>();  // 예전 형식 {"metric": "이름"}
            } else if (asked instanceof String s) {
                askedList = new ArrayList<>(List.of(s));
            } else {
                askedList = Dyn.items(asked);
            }
            List<String> chosen = new ArrayList<>();
            for (Object n : askedList) {
                Map<String, Object> metric = metricNames.get(squash(n));
                if (metric == null) {
                    throw new IllegalArgumentException("'" + Py.str(n) + "' 는 [지표] 에 없는 지표 이름입니다. 이름을 그대로 적거나 빈 목록으로 답하십시오");
                }
                if (!chosen.contains((String) metric.get("name"))) {
                    chosen.add((String) metric.get("name"));
                    // 지표를 골랐으면 지표의 테이블도 쓴다
                    for (String t : Dyn.strings(metric.get("tables"))) {
                        if (!picked.contains(t)) {
                            picked.add(t);
                        }
                    }
                }
            }
            if (picked.isEmpty() && Py.text(p.get("clarify")).strip().isEmpty()) {
                throw new IllegalArgumentException("\"tables\" 가 비었습니다");
            }
            // 없는 계산은 지표 초안의 출발점일 뿐이라 틀린 항목은 다시 묻지 않고 버린다
            List<Map<String, Object>> missing = new ArrayList<>();
            for (Object raw : Dyn.items(p.get("missing"))) {
                Map<String, Object> m = Dyn.map(raw);
                if (m == null || Py.text(m.get("name")).strip().isEmpty()) {
                    continue;
                }
                String table = names.get(Py.text(m.get("table")).strip().toLowerCase());
                if (table == null) {
                    continue;
                }
                String column = columns.get(table).getOrDefault(Py.text(m.get("column")).strip().toLowerCase(), "");
                missing.add(Dyn.of("name", Py.str(m.get("name")).strip(), "table", table, "column", column));
            }
            Map<String, Object> out = new LinkedHashMap<>(p);
            out.put("tables", picked);
            out.put("metrics", chosen);
            out.put("missing", missing);
            return out;
        });
        String reason = Py.text(parsed.get("reason"));
        String clarify = Py.text(parsed.get("clarify")).strip();
        @SuppressWarnings("unchecked")
        List<String> pickedTables = (List<String>) parsed.get("tables");
        @SuppressWarnings("unchecked")
        List<String> chosenMetrics = (List<String>) parsed.get("metrics");
        boolean needsMetric = Py.truthy(parsed.get("needs_metric")) || !chosenMetrics.isEmpty();
        step.selected = new ArrayList<>(pickedTables);
        for (String name : chosenMetrics) {
            step.selected.add("지표: " + name);
        }
        step.reason = reason;
        List<Map<String, Object>> missing = needsMetric && chosenMetrics.isEmpty() ? Dyn.maps(parsed.get("missing")) : new ArrayList<>();
        log.info("{} 테이블 선택 결과: tables={} needs_metric={} metrics={}{}{}", LlmCalls.LLM_LOG, pickedTables, needsMetric,
                chosenMetrics, missing.isEmpty() ? "" : " 없는 지표=" + missing, clarify.isEmpty() ? "" : " 되묻기=" + clarify);
        return new Selection(pickedTables, chosenMetrics, needsMetric, reason, step, clarify.isEmpty() ? null : clarify, missing);
    }
}
