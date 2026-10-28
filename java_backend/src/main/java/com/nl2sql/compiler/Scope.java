package com.nl2sql.compiler;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

import com.nl2sql.common.Json;

/** FROM 절이 보이는 이름들. {별칭: {컬럼: UTC 시차(없으면 0)}} */
class Scope {

    /** 컬럼 하나: 별칭, 컬럼, 시차 */
    record Resolved(String alias, String col, int hours) {
    }

    final Dialect dialect;
    final Map<String, Map<String, Integer>> sources = new LinkedHashMap<>();
    final Map<String, String> real = new LinkedHashMap<>();  // 별칭 → 실제 테이블 (하위 쿼리는 없다)
    final Map<String, Map<String, String>> families = new LinkedHashMap<>();  // 별칭 → {컬럼: 타입 갈래}

    Scope(Dialect dialect) {
        this.dialect = dialect;
    }

    void add(String alias, Map<String, Integer> columns, Map<String, String> columnFamilies) {
        if (sources.containsKey(alias)) {
            throw new AstError("FROM 의 테이블·별칭이 겹칩니다 -> " + alias);
        }
        sources.put(alias, new LinkedHashMap<>(columns));
        families.put(alias, columnFamilies == null ? new LinkedHashMap<>() : new LinkedHashMap<>(columnFamilies));
    }

    String family(String alias, String col) {
        Map<String, String> f = families.get(alias);
        return f == null ? null : f.get(col);
    }

    Resolved resolve(Object ref) {
        // on 에 {"col": "a.컬럼"}, {"expr": {"col": "a.컬럼"}} 처럼 식으로 감싸 적어도 받는다
        while (ref instanceof Map<?, ?> m && m.size() == 1 && (m.containsKey("col") || m.containsKey("expr"))) {
            ref = m.containsKey("col") ? m.get("col") : m.get("expr");
        }
        if (!(ref instanceof String text)) {
            throw new AstError("컬럼은 '테이블.컬럼' 문자열로 적습니다 -> " + Json.dumps(ref));
        }
        text = text.strip();
        if (text.contains(".")) {
            // 별칭에도 점이 있을 수 있다 ('스키마.테이블') — 가장 길게 맞는 별칭을 고른다
            String alias = text.split("\\.", 2)[0];
            for (String name : sources.keySet()) {
                if (text.startsWith(name + ".") && name.length() > alias.length()) {
                    alias = name;
                }
            }
            if (!sources.containsKey(alias)) {
                // 별칭을 붙인 테이블을 실제 이름으로 가리켰다 — 그 테이블의 별칭이 하나뿐이면 그 별칭으로 본다
                String table = "";
                for (String r : real.values()) {
                    if (text.startsWith(r + ".") && r.length() > table.length()) {
                        table = r;
                    }
                }
                List<String> aliases = new ArrayList<>();
                for (Map.Entry<String, String> e : real.entrySet()) {
                    if (!table.isEmpty() && e.getValue().equals(table)) {
                        aliases.add(e.getKey());
                    }
                }
                if (aliases.size() == 1) {
                    return resolve(aliases.get(0) + "." + text.substring(table.length() + 1));
                }
                List<String> known = new ArrayList<>();
                for (String name : sources.keySet()) {
                    String r = real.getOrDefault(name, name);
                    known.add(r.equals(name) ? name : name + "(" + r + ")");
                }
                throw new AstError("FROM 에 없는 테이블입니다 -> " + alias + " (있는 것: " + String.join(", ", known) + ")");
            }
            String col = text.substring(alias.length() + 1);
            if (!sources.get(alias).containsKey(col)) {
                throw new AstError(alias + " 에 없는 컬럼입니다 -> " + col);
            }
            return new Resolved(alias, col, sources.get(alias).get(col));
        }
        List<String> owners = new ArrayList<>();
        for (Map.Entry<String, Map<String, Integer>> e : sources.entrySet()) {
            if (e.getValue().containsKey(text)) {
                owners.add(e.getKey());
            }
        }
        if (owners.isEmpty()) {
            TreeSet<String> known = new TreeSet<>();
            for (Map<String, Integer> cols : sources.values()) {
                known.addAll(cols.keySet());
            }
            throw new AstError("없는 컬럼입니다 -> " + text + " (쓸 수 있는 것: " + String.join(", ", known) + ")");
        }
        if (owners.size() > 1) {
            throw new AstError("여러 테이블에 있는 컬럼입니다 -> " + text + " ('테이블.컬럼' 으로 적으십시오)");
        }
        return new Resolved(owners.get(0), text, sources.get(owners.get(0)).get(text));
    }

    String raw(String alias, String col) {
        return sources.size() == 1 ? dialect.q(col) : dialect.q(alias) + "." + dialect.q(col);
    }
}
