package com.nl2sql.service;

import java.security.GeneralSecurityException;
import java.util.Map;

import org.springframework.stereotype.Service;

import com.nl2sql.common.ApiException;
import com.nl2sql.db.MetaDb;
import com.nl2sql.db.Secret;

/** 대상 DB 접속 정보 (systems.connection_row). 시스템 · 지표 서비스가 함께 쓴다 — 서로를 부르지 않게 따로 둔다. */
@Service
public class Connections {

    private final MetaDb db;
    private final Secret secret;

    public Connections(MetaDb db, Secret secret) {
        this.db = db;
        this.secret = secret;
    }

    /** 카탈로그 읽기용 접속 정보. 비밀번호는 여기서만 복호화한다. */
    public Map<String, Object> row(String systemId) {
        Map<String, Object> row = db.one("SELECT * FROM meta_system WHERE id = ?", systemId);
        if (row == null) {
            throw new ApiException(404, "시스템을 찾을 수 없습니다");
        }
        try {
            row.put("password", secret.decrypt((byte[]) row.get("password_enc")));
        } catch (GeneralSecurityException | RuntimeException error) {
            if (error instanceof IllegalStateException) {
                throw (IllegalStateException) error;  // 키 설정 오류는 그대로
            }
            throw new ApiException(400, row.get("name") + " 의 비밀번호를 풀 수 없습니다 (키가 바뀌었으면 비밀번호를 다시 입력하십시오)");
        }
        return row;
    }
}
