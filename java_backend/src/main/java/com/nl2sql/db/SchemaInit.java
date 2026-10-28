package com.nl2sql.db;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.InitializingBean;
import org.springframework.core.io.ClassPathResource;
import org.springframework.stereotype.Component;

/**
 * 기동 준비 (app.py 의 lifespan): 비밀번호 키를 확인하고 schema.sql 을 돌린다. 웹 서버가 요청을 받기 전에 끝난다.
 *
 * 실패하면 기동을 멈춘다. 로그만 남기고 계속 뜨면 화면은 멀쩡해 보이는데
 * 모든 요청이 깨지고, 원인이 기동 로그 한 줄에만 남는다.
 */
@Component
public class SchemaInit implements InitializingBean {

    private static final Logger log = LoggerFactory.getLogger("nl2sql");

    private final MetaDb db;
    private final Secret secret;

    public SchemaInit(MetaDb db, Secret secret) {
        this.db = db;
        this.secret = secret;
    }

    @Override
    public void afterPropertiesSet() {
        secret.check();
        try {
            db.script(new ClassPathResource("schema.sql").getContentAsString(StandardCharsets.UTF_8));
        } catch (IOException error) {
            throw new UncheckedIOException("schema.sql 을 읽지 못했습니다", error);
        }
        log.info("Database schema initialized");
    }
}
