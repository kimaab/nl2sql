package com.nl2sql.llm;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.SystemMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.model.chat.response.ChatResponse;
import dev.langchain4j.model.openai.OpenAiChatModel;
import dev.langchain4j.model.output.TokenUsage;

/** LangChain4j (OpenAI 호환) 로 부른다 */
public class LangChain4jLlm implements Llm {

    private final OpenAiChatModel model;
    private final String modelName;

    public LangChain4jLlm(String baseUrl, String apiKey, String modelName, double temperature) {
        OpenAiChatModel.OpenAiChatModelBuilder builder = OpenAiChatModel.builder()
                .apiKey(apiKey)
                .modelName(modelName)
                .temperature(temperature)
                .timeout(Duration.ofMinutes(5));
        if (baseUrl != null && !baseUrl.isEmpty()) {
            builder.baseUrl(baseUrl);
        }
        this.model = builder.build();
        this.modelName = modelName;
    }

    @Override
    public Reply invoke(List<Llm.Message> messages) {
        List<ChatMessage> chat = new ArrayList<>();
        for (Llm.Message m : messages) {
            switch (m.role()) {
                case "system" -> chat.add(SystemMessage.from(m.content()));
                case "assistant" -> chat.add(AiMessage.from(m.content()));
                default -> chat.add(UserMessage.from(m.content()));
            }
        }
        ChatResponse response = model.chat(chat);
        String text = response.aiMessage() == null ? "" : response.aiMessage().text();
        TokenUsage usage = response.tokenUsage();
        int promptTokens = usage == null || usage.inputTokenCount() == null ? 0 : usage.inputTokenCount();
        int completionTokens = usage == null || usage.outputTokenCount() == null ? 0 : usage.outputTokenCount();
        return new Reply(text == null ? "" : text, promptTokens, completionTokens);
    }

    @Override
    public String modelName() {
        return modelName;
    }
}
