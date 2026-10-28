package com.nl2sql.common;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.math.MathContext;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** 파이썬 쪽과 같은 문자열을 만드는 작은 도구들 (오류 메시지, 프롬프트, SQL 의 숫자 표기). */
public final class Py {

    private Py() {
    }

    /** 오류 메시지 첫 줄. ApiException 은 detail 을 쓴다. 비었으면 예외 이름. */
    public static String firstLine(Throwable error) {
        String text = error instanceof ApiException api ? api.detail() : error.getMessage();
        if (text != null) {
            for (String line : text.strip().split("\\R")) {
                if (!line.isEmpty()) {
                    return line;
                }
            }
        }
        return error.getClass().getSimpleName();
    }

    /** " ".join(text.split()) — 공백을 하나로 모으고 앞뒤를 자른다 */
    public static String squeeze(Object value) {
        String text = value == null ? "" : String.valueOf(value);
        StringBuilder out = new StringBuilder();
        for (String part : text.strip().split("\\s+")) {
            if (part.isEmpty()) {
                continue;
            }
            if (!out.isEmpty()) {
                out.append(' ');
            }
            out.append(part);
        }
        return out.toString();
    }

    /** 값이 비었는가 (파이썬의 거짓 값: None, "", 0, 빈 목록 · 객체, False) */
    public static boolean truthy(Object value) {
        if (value == null) {
            return false;
        }
        if (value instanceof Boolean b) {
            return b;
        }
        if (value instanceof String s) {
            return !s.isEmpty();
        }
        if (value instanceof Number n) {
            return n.doubleValue() != 0;
        }
        if (value instanceof List<?> l) {
            return !l.isEmpty();
        }
        if (value instanceof Map<?, ?> m) {
            return !m.isEmpty();
        }
        return true;
    }

    /** str(value or "") */
    public static String text(Object value) {
        if (!truthy(value)) {
            return "";
        }
        return value instanceof Number n ? repr(n) : String.valueOf(value);
    }

    /** str(value) — None, True, 숫자를 파이썬처럼 */
    public static String str(Object value) {
        if (value == null) {
            return "None";
        }
        if (value instanceof Boolean b) {
            return b ? "True" : "False";
        }
        if (value instanceof Number n) {
            return repr(n);
        }
        return String.valueOf(value);
    }

    /** 정수인 숫자 (bool 은 아니다) */
    public static boolean isInt(Object value) {
        return value instanceof Integer || value instanceof Long || value instanceof Short
                || value instanceof Byte || value instanceof BigInteger;
    }

    /** 숫자 (정수 또는 소수) */
    public static boolean isNumber(Object value) {
        return value instanceof Number;
    }

    /** 파이썬 repr(숫자): 정수는 그대로, 소수는 1.0 · 0.5 · 1e-05 · 1e+16 모양 */
    public static String repr(Number value) {
        if (isInt(value)) {
            return value.toString();
        }
        if (value instanceof BigDecimal big) {
            return repr(big.doubleValue());
        }
        double d = value.doubleValue();
        if (Double.isNaN(d)) {
            return "nan";
        }
        if (Double.isInfinite(d)) {
            return d > 0 ? "inf" : "-inf";
        }
        if (d == 0) {
            return (1 / d < 0) ? "-0.0" : "0.0";
        }
        BigDecimal exact = new BigDecimal(Double.toString(d));
        int exponent = exact.precision() - exact.scale() - 1;
        if (exponent < -4 || exponent >= 16) {
            BigDecimal unscaled = exact.movePointLeft(exponent).stripTrailingZeros();
            String mantissa = unscaled.toPlainString();
            String sign = exponent < 0 ? "-" : "+";
            int abs = Math.abs(exponent);
            return mantissa + "e" + sign + (abs < 10 ? "0" + abs : String.valueOf(abs));
        }
        String plain = exact.stripTrailingZeros().toPlainString();
        return plain.contains(".") ? plain : plain + ".0";
    }

    /** f"{n:g}" — 유효숫자 6자리, 뒤의 0 을 뗀다 */
    public static String g(double value) {
        if (value == 0) {
            return "0";
        }
        BigDecimal rounded = new BigDecimal(value).round(new MathContext(6, RoundingMode.HALF_EVEN));
        int exponent = rounded.precision() - rounded.scale() - 1;
        if (exponent < -4 || exponent >= 6) {
            BigDecimal mantissa = rounded.movePointLeft(exponent).stripTrailingZeros();
            int abs = Math.abs(exponent);
            return mantissa.toPlainString() + "e" + (exponent < 0 ? "-" : "+") + (abs < 10 ? "0" + abs : abs);
        }
        return rounded.stripTrailingZeros().toPlainString();
    }

    /** f"{n:,}" — 천 단위 쉼표 */
    public static String comma(long value) {
        return String.format("%,d", value);
    }

    /** f"{n:+d}" */
    public static String signed(long value) {
        return value >= 0 ? "+" + value : String.valueOf(value);
    }

    /** 문자열 목록을 sep 로 잇는다 */
    public static String join(String sep, List<?> items) {
        List<String> texts = new ArrayList<>();
        for (Object item : items) {
            texts.add(String.valueOf(item));
        }
        return String.join(sep, texts);
    }
}
