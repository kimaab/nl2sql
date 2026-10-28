package com.nl2sql.common;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import tools.jackson.core.type.TypeReference;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.PropertyNamingStrategies;
import tools.jackson.databind.json.JsonMapper;

/**
 * JSON 도구. AST · 계약서 같은 자유로운 모양은 Map · List 로 다룬다 (파이썬의 dict · list 처럼).
 * dumps 는 파이썬 json.dumps(ensure_ascii=False) 와 같은 모양으로 쓴다 — 프롬프트와 기록에 실리는 글이 같게.
 */
public final class Json {

    /** 레코드는 snake_case 로 (API 응답과 같다). Map 의 키는 그대로다. */
    public static final JsonMapper MAPPER = JsonMapper.builder()
            .propertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE)
            .disable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
            .build();

    private Json() {
    }

    /** JSON 글 → Map · List · String · Number · Boolean · null */
    public static Object parse(String text) {
        return MAPPER.readValue(text, Object.class);
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object value = parse(text);
        if (!(value instanceof Map)) {
            throw new IllegalArgumentException("JSON 객체가 아닙니다");
        }
        return (Map<String, Object>) value;
    }

    /** 레코드 등 → Map · List 모양 (model_dump) */
    public static Object plain(Object value) {
        if (value == null) {
            return null;
        }
        return MAPPER.convertValue(value, Object.class);
    }

    public static <T> T convert(Object value, Class<T> type) {
        return MAPPER.convertValue(value, type);
    }

    public static <T> T convert(Object value, TypeReference<T> type) {
        return MAPPER.convertValue(value, type);
    }

    /** 레코드 · Map · List 를 파이썬 json.dumps(ensure_ascii=False) 모양의 글로 */
    public static String dumps(Object value) {
        StringBuilder out = new StringBuilder();
        write(plainTree(value), out);
        return out.toString();
    }

    private static Object plainTree(Object value) {
        if (value == null || value instanceof String || value instanceof Number || value instanceof Boolean
                || value instanceof Map || value instanceof List) {
            return value;
        }
        return plain(value);
    }

    private static void write(Object value, StringBuilder out) {
        if (value == null) {
            out.append("null");
        } else if (value instanceof Boolean b) {
            out.append(b ? "true" : "false");
        } else if (value instanceof Number n) {
            out.append(Py.repr(n));
        } else if (value instanceof String s) {
            quote(s, out);
        } else if (value instanceof Map<?, ?> map) {
            out.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                if (!first) {
                    out.append(", ");
                }
                first = false;
                quote(String.valueOf(e.getKey()), out);
                out.append(": ");
                write(plainTree(e.getValue()), out);
            }
            out.append('}');
        } else if (value instanceof List<?> list) {
            out.append('[');
            boolean first = true;
            for (Object item : list) {
                if (!first) {
                    out.append(", ");
                }
                first = false;
                write(plainTree(item), out);
            }
            out.append(']');
        } else {
            quote(String.valueOf(value), out);
        }
    }

    private static void quote(String s, StringBuilder out) {
        out.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                case '\b' -> out.append("\\b");
                case '\f' -> out.append("\\f");
                default -> {
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
                }
            }
        }
        out.append('"');
    }

    /** 깊은 복사 (Map · List 만 새로 만든다) */
    @SuppressWarnings("unchecked")
    public static Object copy(Object value) {
        if (value instanceof Map<?, ?> map) {
            Map<String, Object> out = new LinkedHashMap<>();
            for (Map.Entry<?, ?> e : map.entrySet()) {
                out.put(String.valueOf(e.getKey()), copy(e.getValue()));
            }
            return out;
        }
        if (value instanceof List<?> list) {
            List<Object> out = new ArrayList<>();
            for (Object item : list) {
                out.add(copy(item));
            }
            return out;
        }
        return value;
    }

    /** 파이썬의 == 비교. 숫자는 값으로 견준다 (1 == 1.0). */
    public static boolean same(Object a, Object b) {
        if (a == null || b == null) {
            return a == b;
        }
        if (a instanceof Number x && b instanceof Number y && !(a instanceof Boolean) && !(b instanceof Boolean)) {
            return Double.compare(x.doubleValue(), y.doubleValue()) == 0
                    || x.doubleValue() == y.doubleValue();
        }
        if (a instanceof Map<?, ?> x && b instanceof Map<?, ?> y) {
            if (x.size() != y.size()) {
                return false;
            }
            for (Map.Entry<?, ?> e : x.entrySet()) {
                if (!y.containsKey(e.getKey()) || !same(e.getValue(), y.get(e.getKey()))) {
                    return false;
                }
            }
            return true;
        }
        if (a instanceof List<?> x && b instanceof List<?> y) {
            if (x.size() != y.size()) {
                return false;
            }
            for (int i = 0; i < x.size(); i++) {
                if (!same(x.get(i), y.get(i))) {
                    return false;
                }
            }
            return true;
        }
        return a.equals(b);
    }
}
