package com.nl2sql.llm;

import java.util.ArrayList;
import java.util.List;

import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.messages.UserMessage;
import org.springframework.ai.chat.metadata.Usage;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.openai.OpenAiChatModel;
import org.springframework.ai.openai.OpenAiChatOptions;

/** Spring AI (OpenAI 호환) 로 부른다 */
public class SpringAiLlm implements Llm {

    private final OpenAiChatModel model;
    private final String modelName;

    public SpringAiLlm(String baseUrl, String apiKey, String modelName, double temperature) {
        OpenAiChatOptions.Builder options = OpenAiChatOptions.builder()
                .apiKey(apiKey)
                .model(modelName)
                .temperature(temperature);
        if (baseUrl != null && !baseUrl.isEmpty()) {
            options.baseUrl(baseUrl);
        }
        this.model = OpenAiChatModel.builder().options(options.build()).build();
        this.modelName = modelName;
    }

    @Override
    public Reply invoke(List<Llm.Message> messages) {
        List<org.springframework.ai.chat.messages.Message> prompt = new ArrayList<>();
        for (Llm.Message m : messages) {
            switch (m.role()) {
                case "system" -> prompt.add(new SystemMessage(m.content()));
                case "assistant" -> prompt.add(new AssistantMessage(m.content()));
                default -> prompt.add(new UserMessage(m.content()));
            }
        }
        ChatResponse response = model.call(new Prompt(prompt));
        String text = response.getResult() == null ? "" : response.getResult().getOutput().getText();
        int promptTokens = 0;
        int completionTokens = 0;
        Usage usage = response.getMetadata() == null ? null : response.getMetadata().getUsage();
        if (usage != null) {
            promptTokens = usage.getPromptTokens() == null ? 0 : usage.getPromptTokens();
            completionTokens = usage.getCompletionTokens() == null ? 0 : usage.getCompletionTokens();
        }
        return new Reply(text == null ? "" : text, promptTokens, completionTokens);
    }

    @Override
    public String modelName() {
        return modelName;
    }
}
