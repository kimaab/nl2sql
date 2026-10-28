package com.nl2sql.common;

import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/** 설정 값. 환경변수와 .env (application.yml 의 spring.config.import) 를 같은 이름으로 읽는다. */
@Component
public class Env {

    private final Environment environment;

    public Env(Environment environment) {
        this.environment = environment;
    }

    /** 값. 없으면 fallback. .env 에 따옴표로 감싼 값은 따옴표를 뗀다. */
    public String get(String name, String fallback) {
        String value = environment.getProperty(name);
        if (value == null) {
            return fallback;
        }
        value = value.strip();
        if (value.length() >= 2) {
            char first = value.charAt(0);
            char last = value.charAt(value.length() - 1);
            if ((first == '"' || first == '\'') && first == last) {
                value = value.substring(1, value.length() - 1);
            }
        }
        return value;
    }

    public String get(String name) {
        return get(name, "");
    }

    public boolean flag(String name) {
        String value = get(name).toLowerCase();
        return value.equals("1") || value.equals("true") || value.equals("yes");
    }
}
