package com.nl2sql.common;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Map · List 로 된 값(AST, 계약서, LLM 응답)을 읽는 도구. 모양이 다르면 null 이나 빈 값을 준다. */
public final class Dyn {

    private Dyn() {
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> map(Object value) {
        return value instanceof Map ? (Map<String, Object>) value : null;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> list(Object value) {
        return value instanceof List ? (List<Object>) value : null;
    }

    /** 목록 (없으면 빈 목록) */
    public static List<Object> items(Object value) {
        List<Object> list = list(value);
        return list == null ? new ArrayList<>() : list;
    }

    /** 객체 목록 (객체가 아닌 항목은 뺀다) */
    public static List<Map<String, Object>> maps(Object value) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Object item : items(value)) {
            Map<String, Object> m = map(item);
            if (m != null) {
                out.add(m);
            }
        }
        return out;
    }

    public static Object get(Map<String, Object> map, String key) {
        return map == null ? null : map.get(key);
    }

    /** 문자열 값 (문자열이 아니면 null) */
    public static String str(Map<String, Object> map, String key) {
        Object value = get(map, key);
        return value instanceof String s ? s : null;
    }

    /** str(map.get(key) or "") */
    public static String text(Map<String, Object> map, String key) {
        return Py.text(get(map, key));
    }

    /** 문자열 목록 */
    public static List<String> strings(Object value) {
        List<String> out = new ArrayList<>();
        for (Object item : items(value)) {
            out.add(String.valueOf(item));
        }
        return out;
    }

    public static boolean bool(Map<String, Object> map, String key) {
        return Py.truthy(get(map, key));
    }

    public static int integer(Map<String, Object> map, String key) {
        Object value = get(map, key);
        return value instanceof Number n ? n.intValue() : 0;
    }

    /** {"k": v, ...} 를 순서대로 */
    public static Map<String, Object> of(Object... pairs) {
        Map<String, Object> out = new LinkedHashMap<>();
        for (int i = 0; i + 1 < pairs.length; i += 2) {
            out.put((String) pairs[i], pairs[i + 1]);
        }
        return out;
    }

    /** 키가 정확히 이것뿐인가 (set(d) == {key}) */
    public static boolean onlyKey(Map<String, Object> map, String key) {
        return map != null && map.size() == 1 && map.containsKey(key);
    }
}
