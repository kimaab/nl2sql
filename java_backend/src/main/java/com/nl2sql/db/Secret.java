package com.nl2sql.db;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.SecureRandom;
import java.util.Arrays;
import java.util.Base64;

import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

import org.springframework.stereotype.Component;

import com.nl2sql.common.Env;

/**
 * 대상 DB 비밀번호 암호화. 동기화 때 실제로 접속해야 하므로 복원 가능해야 한다 (AES-256-GCM).
 *
 * 키는 NL2SQL_SECRET_KEY (32바이트를 base64 로). 파이썬 secret.py 와 같은 형식이라 서로 푼다: nonce(12) + 암호문 + 태그(16).
 * 키를 잃으면 저장된 비밀번호를 풀 수 없다 — 시스템마다 비밀번호를 다시 입력해야 한다.
 */
@Component
public class Secret {

    private static final int NONCE = 12;
    private static final int TAG_BITS = 128;
    private static final SecureRandom RANDOM = new SecureRandom();

    private final Env env;

    public Secret(Env env) {
        this.env = env;
    }

    private byte[] key() {
        String raw = env.get("NL2SQL_SECRET_KEY");
        if (raw.isEmpty()) {
            throw new IllegalStateException("NL2SQL_SECRET_KEY 가 없습니다 (backend/.env). 만드는 법은 backend/secret.py 맨 위 주석");
        }
        byte[] key;
        try {
            key = Base64.getDecoder().decode(raw);
        } catch (IllegalArgumentException error) {
            throw new IllegalStateException("NL2SQL_SECRET_KEY 는 base64 여야 합니다");
        }
        if (key.length != 32) {
            throw new IllegalStateException("NL2SQL_SECRET_KEY 는 32바이트여야 합니다 (받은 것: " + key.length + "바이트)");
        }
        return key;
    }

    /** 기동 때 키가 쓸 만한지 확인한다 — 첫 동기화에서야 터지면 원인을 찾기 어렵다. */
    public void check() {
        key();
    }

    public byte[] encrypt(String plain) {
        byte[] nonce = new byte[NONCE];
        RANDOM.nextBytes(nonce);
        try {
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key(), "AES"), new GCMParameterSpec(TAG_BITS, nonce));
            byte[] sealed = cipher.doFinal(plain.getBytes(StandardCharsets.UTF_8));
            byte[] out = Arrays.copyOf(nonce, NONCE + sealed.length);
            System.arraycopy(sealed, 0, out, NONCE, sealed.length);
            return out;
        } catch (GeneralSecurityException error) {
            throw new IllegalStateException("비밀번호를 암호화하지 못했습니다", error);
        }
    }

    /** 키가 틀리거나 값이 깨졌으면 GeneralSecurityException */
    public String decrypt(byte[] blob) throws GeneralSecurityException {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key(), "AES"),
                new GCMParameterSpec(TAG_BITS, Arrays.copyOf(blob, NONCE)));
        byte[] plain = cipher.doFinal(blob, NONCE, blob.length - NONCE);
        return new String(plain, StandardCharsets.UTF_8);
    }
}
