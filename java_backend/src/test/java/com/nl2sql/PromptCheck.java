package com.nl2sql;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.nl2sql.common.Dyn;
import com.nl2sql.common.Json;
import com.nl2sql.llm.Llm;
import com.nl2sql.service.AskService;
import com.nl2sql.service.Selector;

/**
 * 파이썬과 프롬프트를 대조하는 도구. 입력: {contract, question, user_id, tables, metrics, now}.
 * 테이블 선택과 조회 명세 단계에서 LLM 에 보내는 첫 메시지(시스템 · 사용자)를 그대로 낸다.
 *
 *   java PromptCheck 입력.json 출력.json
 */
public class PromptCheck {

    public static void main(String[] args) throws Exception {
        Map<String, Object> in = Dyn.map(Json.parse(Files.readString(Path.of(args[0]))));
        Map<String, Object> contract = Dyn.map(in.get("contract"));
        String question = AskService.withUser((String) in.get("question"), (String) in.get("user_id"));

        // 테이블 선택: 첫 요청의 메시지를 받아 두고, 고를 테이블을 정해 답한다
        List<List<Llm.Message>> sent = new ArrayList<>();
        Llm recorder = new Llm() {
            @Override
            public Reply invoke(List<Message> messages) {
                sent.add(new ArrayList<>(messages));
                return new Reply(Json.dumps(Dyn.of("tables", in.get("tables"), "metrics", new ArrayList<>())), 0, 0);
            }

            @Override
            public String modelName() {
                return "check";
            }
        };
        Selector.selectTables(recorder, contract, question);

        List<Map<String, Object>> metrics = new ArrayList<>();
        for (Object name : Dyn.items(in.get("metrics"))) {
            for (Map<String, Object> m : Dyn.maps(contract.get("metrics"))) {
                if (name.equals(m.get("name"))) {
                    metrics.add(m);
                }
            }
        }
        String[] ast = AskService.astPrompt(contract, Dyn.strings(in.get("tables")), metrics,
                LocalDateTime.parse((String) in.get("now")));

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("table_system", sent.get(0).get(0).content());
        out.put("table_user", sent.get(0).get(1).content());
        out.put("ast_system", ast[0]);
        out.put("ast_user", ast[1] + "\n\n[질문]\n" + question);
        Files.writeString(Path.of(args[1]), Json.dumps(out));
    }
}
