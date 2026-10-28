package com.nl2sql.llm;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import com.nl2sql.common.Json;

/** LLM 을 부르는 공용 도구: 한 번 부르기 · JSON 꺼내기 · 형식이 틀리면 다시 묻기 (selector.py 의 공통 부분) */
public final class LlmCalls {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.selector");
    public static final String LLM_LOG = "[LLM 연동]";
    private static final Map<String, String> STAGE_NAMES = Map.of("table", "테이블 선택", "sql", "조회 명세", "draft", "지표 초안");
    private static final Pattern FENCED = Pattern.compile("```(?:json)?\\s*(.*?)```", Pattern.DOTALL);

    private LlmCalls() {
    }

    public static String stageName(Step step) {
        return STAGE_NAMES.getOrDefault(step.stage, step.stage);
    }

    /** 모델 응답에서 JSON 객체 하나를 꺼낸다 (코드펜스·앞뒤 말 허용). 없으면 IllegalArgumentException. */
    public static Map<String, Object> parseJson(String text) {
        String body = text == null ? "" : text.strip();
        Matcher fenced = FENCED.matcher(body);
        if (fenced.find()) {
            body = fenced.group(1).strip();
        }
        int start = body.indexOf('{');
        int end = body.lastIndexOf('}');
        if (start < 0 || end <= start) {
            throw new IllegalArgumentException("JSON 객체가 없습니다");
        }
        Object parsed;
        try {
            parsed = Json.parse(body.substring(start, end + 1));
        } catch (RuntimeException error) {
            throw new IllegalArgumentException(firstLine(error.getMessage()));
        }
        if (!(parsed instanceof Map)) {
            throw new IllegalArgumentException("JSON 객체가 아닙니다");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> out = (Map<String, Object>) parsed;
        return out;
    }

    /** 모델을 한 번 부른다. 요청·응답을 로그에 남기고 토큰·시간은 step 에 더한다. */
    public static Llm.Reply chat(Llm model, List<Llm.Message> messages, Step step, int attempt) {
        String stage = stageName(step);
        if (attempt == 1) {
            log.info("{} {} 요청 attempt=1 model={}\n[system]\n{}\n[user]\n{}", LLM_LOG, stage, step.model,
                    messages.get(0).content(), messages.get(messages.size() - 1).content());
        } else {
            log.info("{} {} 재요청 attempt={}\n[user]\n{}", LLM_LOG, stage, attempt, messages.get(messages.size() - 1).content());
        }
        long started = System.nanoTime();
        Llm.Reply reply = model.invoke(messages);
        int elapsed = (int) ((System.nanoTime() - started) / 1_000_000);
        step.elapsedMs += elapsed;
        step.promptTokens += reply.promptTokens();
        step.completionTokens += reply.completionTokens();
        log.info("{} {} 응답 attempt={} (토큰 {}+{}, {}ms)\n{}", LLM_LOG, stage, attempt, reply.promptTokens(),
                reply.completionTokens(), elapsed, reply.content());
        return reply;
    }

    /**
     * JSON 으로 답하게 하고, 형식이 틀리면 이유를 붙여 다시 묻는다. validate 는 고친 값을 돌려줘도 된다 (null 이면 그대로).
     * 형식 오류는 validate 가 IllegalArgumentException · ClassCastException 등으로 알린다.
     */
    public static Map<String, Object> askJson(Llm model, String system, String user, Step step,
            Function<Map<String, Object>, Map<String, Object>> validate, int tries) {
        List<Llm.Message> messages = new ArrayList<>(List.of(Llm.Message.system(system), Llm.Message.user(user)));
        String lastError = null;
        for (int attempt = 1; attempt <= tries; attempt++) {
            Llm.Reply reply = chat(model, messages, step, attempt);
            try {
                Map<String, Object> parsed = parseJson(reply.content());
                Map<String, Object> fixed = validate.apply(parsed);
                return fixed != null ? fixed : parsed;
            } catch (IllegalArgumentException | ClassCastException | NullPointerException error) {
                log.info("{} 응답 형식 오류 attempt={}: {}", LLM_LOG, attempt, error.getMessage());
                lastError = error.getMessage();
                messages.add(Llm.Message.assistant(reply.content()));
                messages.add(Llm.Message.user("형식이 틀렸습니다: " + error.getMessage()
                        + ". 설명 없이 JSON 객체 하나만 다시 답하십시오."));
            }
        }
        throw new IllegalArgumentException(stageName(step) + " 응답을 읽지 못했습니다: " + lastError);
    }

    public static Map<String, Object> askJson(Llm model, String system, String user, Step step,
            Function<Map<String, Object>, Map<String, Object>> validate) {
        return askJson(model, system, user, step, validate, 2);
    }

    private static String firstLine(String text) {
        if (text == null) {
            return "JSON 을 읽지 못했습니다";
        }
        String[] lines = text.strip().split("\\R");
        return lines.length == 0 ? text : lines[0];
    }
}
