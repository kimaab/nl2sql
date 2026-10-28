package com.nl2sql.compiler;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import com.nl2sql.common.Dyn;
import com.nl2sql.common.Json;
import com.nl2sql.common.Py;

/**
 * 조회 명세(AST) → SQL. 조합하고 검증만 한다 — 무엇을 어떻게 계산할지는 LLM 과 지표가 정한다 (compiler.py).
 *
 * 예외는 하나: UTC 로 저장된 시각 컬럼(utc_offset)은 현지 시각으로 자동 보정한다. AST 의 시각은 모두 현지 시각이다.
 *   - 컬럼을 쓰면 (컬럼 + 시차) 로 바꾼다 — 날짜 묶음·표시가 현지 기준이 된다.
 *   - 컬럼을 상수와 비교하면 반대로 상수를 UTC 로 바꾼다 — 컬럼을 감싸지 않아야 인덱스를 탄다.
 *
 * AST 모양
 *   {"with": [{"name": "이름", "query": AST}],   맨 바깥에만. 정의한 이름은 from · joins 에서 테이블처럼 쓴다
 *    "from": "테이블" | {"table": "테이블", "as": "별칭"} | {"query": AST, "as": "별칭"},
 *    "joins": [{"table": "테이블" | "query": AST | "tree": 계층, "as": "별칭", "type": "inner|left", "on": [["a.컬럼", "b.컬럼"]]}],
 *    "select": [{"expr": 식, "as": "이름"}],        {"expr": {"col": "*"}} 는 모든 컬럼
 *    "distinct": true, "where": [조건], "group_by": [식], "having": [조건],
 *    "order_by": [{"expr": 식, "dir": "asc|desc"}], "limit": 숫자}
 *   식: {"col": "컬럼|테이블.컬럼"} | {"num": 1} | {"str": "문자"} | {"fn": "함수", "args": [식]}
 *   조건: {"expr": 식, "op": "=", "value": 값 또는 식} | {"or": [조건]} | {"and": [조건]}  — 목록은 AND
 *         in · not_in 의 value 는 {"query": AST} (select 하나) 도 된다
 *   계층: {"table": "테이블", "key": "id", "parent": "parent_id", "start": [조건], "direction": "down|up"}
 *         시작 행에서 자식(down) 또는 부모(up) 쪽으로 따라간 행 전부 + depth(시작 = 0). from 이나 joins 에 {"tree": 계층, "as": "별칭"}.
 */
public class Compiler {

    public static final Set<String> AGGREGATES = Set.of("SUM", "AVG", "MIN", "MAX", "COUNT", "COUNT_DISTINCT");
    static final Map<String, String> ARITHMETIC = Map.of("ADD", "+", "SUB", "-", "MUL", "*");
    static final Map<String, int[]> SCALARS = Map.of(  // 이름: (최소, 최대 인자 수)
            "ABS", new int[] {1, 1},
            "ROUND", new int[] {1, 2},
            "COALESCE", new int[] {2, 8},
            "NULLIF", new int[] {2, 2},
            "GREATEST", new int[] {2, 8},
            "LEAST", new int[] {2, 8});
    static final Set<String> OTHER_FUNCTIONS = Set.of("DIV", "DATE", "DATE_TRUNC", "ADD_HOURS", "ADD_DAYS",
            "ADD_MINUTES", "ADD_SECONDS", "NOW", "CURRENT_DATE", "ROW_NUMBER", "CASE");
    static final Map<String, String> INTERVAL_FUNCTIONS =
            Map.of("ADD_SECONDS", "seconds", "ADD_MINUTES", "minutes", "ADD_HOURS", "hours", "ADD_DAYS", "days");
    static final List<String> TRUNC_UNITS = List.of("hour", "day", "week", "month", "year");
    static final Map<String, String> COMPARE = new LinkedHashMap<>();
    static final List<String> CONDITION_OPS = new ArrayList<>();
    static final Set<String> QUERY_KEYS =
            Set.of("from", "joins", "select", "distinct", "where", "group_by", "having", "order_by", "limit");
    public static final int MAX_LIMIT = 10000;
    public static final int MAX_DEPTH = 6;  // 하위 쿼리 깊이. 등록 SQL 의 WITH 를 펼치면 IN (하위 쿼리) 와 겹쳐 깊어진다
    public static final int MAX_TREE_DEPTH = 20;  // 계층을 따라가는 최대 단계. 부모가 자기 자신이거나 고리가 있어도 여기서 끝난다
    static final Set<String> TREE_KEYS = Set.of("table", "key", "parent", "start", "direction");

    static {
        COMPARE.put("=", "=");
        COMPARE.put("!=", "<>");
        COMPARE.put("<>", "<>");
        COMPARE.put(">", ">");
        COMPARE.put(">=", ">=");
        COMPARE.put("<", "<");
        COMPARE.put("<=", "<=");
        CONDITION_OPS.addAll(COMPARE.keySet());
        CONDITION_OPS.addAll(List.of("in", "not_in", "like", "not_like", "between", "is_null", "is_not_null"));
    }

    /** [계산 필요] 컬럼 하나 */
    record Ref(String table, String column) {
    }

    /** 식의 성질. raw: 집계하지 않은 [계산 필요] 컬럼, calc: 집계한 [계산 필요] 컬럼 */
    static final class Info {
        boolean agg;
        boolean window;
        boolean cols;
        List<Ref> raw = new ArrayList<>();
        List<Ref> calc = new ArrayList<>();

        static Info of() {
            return new Info();
        }

        static Info cols(List<Ref> raw) {
            Info info = new Info();
            info.cols = true;
            info.raw = new ArrayList<>(raw);
            return info;
        }

        Info copy() {
            Info out = new Info();
            out.agg = agg;
            out.window = window;
            out.cols = cols;
            out.raw = new ArrayList<>(raw);
            out.calc = new ArrayList<>(calc);
            return out;
        }
    }

    /** 식 하나의 SQL 과 성질 */
    record Part(String sql, Info info) {
    }

    /** 쿼리 한 단계의 SQL 과 select 이름 */
    record Query(String sql, List<String> names) {
    }

    /** with 이름 하나의 컬럼과 타입 갈래 */
    record Cte(Map<String, Integer> columns, Map<String, String> families) {
    }

    private final LocalDateTime utcNow;
    private final Dialect dialect;
    private final Map<String, Map<String, Integer>> tables = new LinkedHashMap<>();
    private final Map<String, String> schemas = new LinkedHashMap<>();  // 기본 스키마가 아닌 테이블 → 스키마
    private final Map<String, Map<String, String>> families = new LinkedHashMap<>();  // 테이블 → {컬럼: 타입 갈래}
    private final Map<String, Set<String>> calcColumns = new LinkedHashMap<>();
    private final Map<String, Set<String>> allowed;  // {컬럼: 지표가 그 컬럼에 쓴 집계 함수} (null 이면 검사하지 않는다)
    private int trees = 0;  // 계층 CTE 이름을 겹치지 않게
    private int level = 0;  // 지금 컴파일하는 쿼리의 깊이 (조건 안 하위 쿼리의 깊이를 센다)
    private Map<String, Cte> ctes = new LinkedHashMap<>();
    private Map<String, String> outputFamilies = new LinkedHashMap<>();  // 마지막으로 컴파일한 쿼리의 {select 이름: 타입 갈래}

    public Compiler(Map<String, Object> contract) {
        this(contract, null, null);
    }

    public Compiler(Map<String, Object> contract, List<Map<String, Object>> metrics) {
        this(contract, metrics, null);
    }

    /**
     * metrics 를 주면 [계산 필요] 컬럼의 계산을 그 지표들의 식으로만 허용한다 (질문 처리).
     * 주지 않으면 검사하지 않는다 (지표 등록 — 지표가 곧 그 계산이다).
     * utcNow 는 UTC 컬럼과 비교한 CURRENT_DATE 를 오늘 날짜로 바꿀 때의 기준 (주지 않으면 지금).
     */
    public Compiler(Map<String, Object> contract, List<Map<String, Object>> metrics, LocalDateTime utcNow) {
        this.utcNow = utcNow;
        this.dialect = new Dialect(Dyn.text(contract, "driver").isEmpty() ? "postgresql" : Dyn.text(contract, "driver"));
        for (Map<String, Object> t : Dyn.maps(contract.get("tables"))) {
            Map<String, Integer> cols = new LinkedHashMap<>();
            Map<String, String> fams = new LinkedHashMap<>();
            Set<String> calc = new HashSet<>();
            for (Map<String, Object> c : Dyn.maps(t.get("columns"))) {
                String name = (String) c.get("name");
                String type = Dyn.text(c, "type");
                fams.put(name, typeFamily(type));
                int hours = Dyn.integer(c, "utc_offset");
                if (hours != 0 && !"datetime".equals(temporalKind(type, dialect.driver()))) {
                    hours = 0;
                }
                cols.put(name, hours);
                if (Dyn.bool(c, "needs_calc")) {
                    calc.add(name);
                }
            }
            String name = (String) t.get("name");
            tables.put(name, cols);
            schemas.put(name, Dyn.text(t, "schema"));
            families.put(name, fams);
            calcColumns.put(name, calc);
        }
        if (metrics != null) {
            allowed = new LinkedHashMap<>();
            for (Map<String, Object> m : metrics) {
                metricAggregates(m.get("ast"), allowed);
            }
        } else {
            allowed = null;
        }
    }

    // ── 공용 판정 (compiler.py 의 모듈 함수) ────────────────────────────────

    /**
     * 컬럼이 날짜형인지, 시각을 포함하는지 판정한다.
     *
     * 'datetime' = 시·분·초를 포함, 'date' = 날짜뿐, null = 날짜 컬럼이 아님.
     * Oracle 의 DATE 는 시각을 포함한다 — 다른 두 드라이버의 date 와 다르다.
     */
    public static String temporalKind(String dataType, String driver) {
        String text = dataType == null ? "" : dataType.strip().toLowerCase();
        if (text.isEmpty()) {
            return null;
        }
        if (text.startsWith("timestamp") || text.startsWith("datetime")) {
            return "datetime";
        }
        if (text.split("\\(")[0].strip().equals("date")) {
            return "oracle".equals(driver) ? "datetime" : "date";
        }
        return null;
    }

    /** 타입 갈래 (number | text | time | 그 밖은 타입 이름). 갈래가 다른 컬럼끼리 비교하면 문자로 바꿔 비교한다. */
    public static String typeFamily(String dataType) {
        String t = dataType == null ? "" : dataType.toLowerCase();
        for (String word : List.of("int", "num", "dec", "float", "double", "real", "serial", "money")) {
            if (t.contains(word)) {
                return "number";
            }
        }
        for (String word : List.of("char", "text", "string", "clob")) {
            if (t.contains(word)) {
                return "text";
            }
        }
        for (String word : List.of("timestamp", "date", "time")) {
            if (t.contains(word)) {
                return "time";
            }
        }
        return t.split("\\(", -1)[0].strip();
    }

    private static final Pattern MOMENT = Pattern.compile(
            "(\\d{4})-(\\d{2})-(\\d{2})(?: (\\d{2})(?::(\\d{2})(?::(\\d{2})(?:\\.(\\d{1,6}))?)?)?)?(Z|[+-]\\d{2}:?\\d{2})?");

    /** 'YYYY-MM-DD' · 'YYYY-MM-DD HH:MM[:SS]' 이면 시각으로, 아니면 null */
    public static LocalDateTime parseDatetime(Object value) {
        String text = Py.text(value).strip().replace("T", " ");
        if (text.length() < 10) {
            return null;
        }
        Matcher m = MOMENT.matcher(text);
        if (!m.matches()) {
            return null;
        }
        try {
            int hour = m.group(4) == null ? 0 : Integer.parseInt(m.group(4));
            int minute = m.group(5) == null ? 0 : Integer.parseInt(m.group(5));
            int second = m.group(6) == null ? 0 : Integer.parseInt(m.group(6));
            int nanos = 0;
            if (m.group(7) != null) {
                nanos = Integer.parseInt((m.group(7) + "000000").substring(0, 6)) * 1000;
            }
            return LocalDateTime.of(Integer.parseInt(m.group(1)), Integer.parseInt(m.group(2)),
                    Integer.parseInt(m.group(3)), hour, minute, second, nanos);
        } catch (java.time.DateTimeException error) {
            return null;
        }
    }

    /**
     * 지표 AST 에서 컬럼마다 쓴 집계 함수. {컬럼: {"MAX", "MIN"}} ('테이블.' 은 뺀다)
     *
     * 식을 그대로 비교하지 않는다 — a - b - c 와 (a - b) - c 처럼 같은 계산이 여러 모양으로 적힌다.
     */
    static void metricAggregates(Object node, Map<String, Set<String>> out) {
        if (node instanceof List<?> list) {
            for (Object x : list) {
                metricAggregates(x, out);
            }
        } else if (node instanceof Map<?, ?> raw) {
            Map<String, Object> m = Dyn.map(raw);
            String fn = Dyn.text(m, "fn").toUpperCase();
            List<Object> args = Dyn.list(m.get("args"));
            if (AGGREGATES.contains(fn) && args != null && args.size() == 1 && args.get(0) instanceof Map) {
                Object col = Dyn.map(args.get(0)).get("col");
                if (col instanceof String s) {
                    String[] parts = s.split("\\.", -1);
                    out.computeIfAbsent(parts[parts.length - 1], k -> new TreeSet<>()).add(fn);
                }
            }
            for (Object value : m.values()) {
                metricAggregates(value, out);
            }
        }
    }

    // ── 컴파일 ──────────────────────────────────────────────────────────────

    public String compile(Object ast) {
        try {
            return compileAst(ast);
        } catch (AstError error) {
            throw error;
        } catch (ClassCastException | NullPointerException | IndexOutOfBoundsException error) {
            throw new AstError("AST 모양이 맞지 않습니다 -> " + error.getMessage());
        }
    }

    private String compileAst(Object ast) {
        ctes = new LinkedHashMap<>();
        Map<String, Object> top = Dyn.map(ast);
        if (top == null || !top.containsKey("with")) {
            return query(ast, 0).sql();
        }
        // with: 한 번 정의한 하위 쿼리를 여러 곳에서 쓴다 (예: 일별 집계를 상위 N 고르기와 상세에 함께)
        List<Object> items = Dyn.list(top.get("with"));
        if (items == null || items.isEmpty()) {
            throw new AstError("with 는 [{\"name\": \"이름\", \"query\": AST}] 목록입니다");
        }
        List<String> parts = new ArrayList<>();
        for (Object raw : items) {
            Map<String, Object> item = Dyn.map(raw);
            if (item == null || !(item.get("name") instanceof String given) || given.strip().isEmpty()) {
                throw new AstError("with 항목은 {\"name\": \"이름\", \"query\": AST} 입니다");
            }
            String name = ((String) item.get("name")).strip();
            if (tables.containsKey(name) || ctes.containsKey(name)) {
                throw new AstError("with 이름이 테이블이나 앞의 with 와 겹칩니다 -> " + name);
            }
            Query inner = query(item.get("query"), 1);
            Map<String, Integer> columns = new LinkedHashMap<>();
            for (String n : inner.names()) {
                columns.put(n, 0);
            }
            ctes.put(name, new Cte(columns, new LinkedHashMap<>(outputFamilies)));
            parts.add(dialect.q(name) + " AS (" + inner.sql() + ")");
        }
        Map<String, Object> body = new LinkedHashMap<>();
        for (Map.Entry<String, Object> e : top.entrySet()) {
            if (!e.getKey().equals("with")) {
                body.put(e.getKey(), e.getValue());
            }
        }
        return "WITH " + String.join(", ", parts) + " " + query(body, 0).sql();
    }

    // ── 쿼리 한 단계. from 에 AST 를 넣으면 하위 쿼리가 된다 ──────────────

    private Query query(Object raw, int depth) {
        Map<String, Object> ast = Dyn.map(raw);
        if (ast == null) {
            throw new AstError("AST 는 JSON 객체입니다");
        }
        if (depth > MAX_DEPTH) {
            throw new AstError("하위 쿼리는 " + MAX_DEPTH + "단계까지입니다");
        }
        TreeSet<String> unknown = new TreeSet<>(ast.keySet());
        unknown.removeAll(QUERY_KEYS);
        if (!unknown.isEmpty()) {
            throw new AstError("모르는 키입니다 -> " + String.join(", ", unknown) + " (쓸 수 있는 키: "
                    + String.join(", ", new TreeSet<>(QUERY_KEYS)) + ")");
        }
        Dialect d = dialect;
        Scope scope = new Scope(d);
        int outerLevel = level;
        level = depth;

        Object source = ast.get("from");
        Map<String, Object> sourceMap = Dyn.map(source);
        String fromSql;
        if (source instanceof String name) {
            fromSql = table(name, scope, null);
        } else if (sourceMap != null && sourceMap.containsKey("query")) {
            fromSql = subquery(sourceMap.get("query"), Py.text(sourceMap.get("as")).isEmpty() ? "sub"
                    : Py.text(sourceMap.get("as")).strip(), scope, depth);
        } else if (sourceMap != null && sourceMap.containsKey("tree")) {
            fromSql = tree(sourceMap.get("tree"), sourceMap.get("as"), scope);
        } else if (sourceMap != null && sourceMap.containsKey("table")) {
            fromSql = table(sourceMap.get("table"), scope, sourceMap.get("as"));
        } else {
            throw new AstError("from 은 테이블 이름이나 {\"table\": \"테이블\", \"as\": \"별칭\"}, {\"query\": AST, \"as\": \"별칭\"}, "
                    + "{\"tree\": 계층, \"as\": \"별칭\"} 입니다");
        }

        for (Object rawJoin : Dyn.items(ast.get("joins"))) {
            Map<String, Object> join = Dyn.map(rawJoin);
            if (join == null) {
                throw new AstError("joins 항목은 {\"table\", \"type\", \"on\"} 또는 {\"query\", \"as\", \"type\", \"on\"} 입니다");
            }
            Object type = join.containsKey("type") ? join.get("type") : "inner";
            String kind = switch (String.valueOf(type).toLowerCase()) {
                case "inner" -> "JOIN";
                case "left" -> "LEFT JOIN";
                default -> throw new AstError("join type 은 inner 나 left 입니다");
            };
            // 하위 쿼리 조인 (예: 합계 상위 5대만 남기기). {"table": {"query", "as"}} 로 적어도 받는다
            Map<String, Object> target = join.get("table") instanceof Map ? Dyn.map(join.get("table")) : join;
            String tableSql;
            Object label;
            if (target.containsKey("query")) {
                String alias = Py.text(target.get("as")).strip();
                if (alias.isEmpty()) {
                    throw new AstError("하위 쿼리 조인에는 \"as\"(별칭)가 있어야 합니다");
                }
                tableSql = subquery(target.get("query"), alias, scope, depth);
                label = alias;
            } else if (target.containsKey("tree")) {
                tableSql = tree(target.get("tree"), target.get("as"), scope);
                label = Py.truthy(target.get("as")) ? target.get("as") : "tree";
            } else {
                tableSql = table(join.get("table"), scope, join.get("as"));
                label = join.get("table");
            }
            List<Object> pairs = Dyn.items(join.get("on"));
            if (pairs.isEmpty()) {
                throw new AstError(label + " 조인에 on 이 없습니다");
            }
            List<String> on = new ArrayList<>();
            for (Object rawPair : pairs) {
                List<Object> pair = Dyn.list(rawPair);
                if (pair == null || pair.size() != 2) {
                    throw new AstError("on 은 [[\"a.컬럼\", \"b.컬럼\"]] 모양입니다");
                }
                List<String> sides = new ArrayList<>();
                List<String> fams = new ArrayList<>();
                for (Object ref : pair) {
                    Scope.Resolved r = scope.resolve(ref);
                    sides.add(scope.raw(r.alias(), r.col()));
                    fams.add(scope.family(r.alias(), r.col()));
                }
                if (Py.truthy(fams.get(0)) && Py.truthy(fams.get(1)) && !fams.get(0).equals(fams.get(1))) {
                    // 예: 정수 id 와 문자 parent_id — PostgreSQL 은 그대로 비교하면 오류다
                    sides = List.of(d.text(sides.get(0)), d.text(sides.get(1)));
                }
                on.add(sides.get(0) + " = " + sides.get(1));
            }
            fromSql += " " + kind + " " + tableSql + " ON " + String.join(" AND ", on);
        }

        List<Object> select = Dyn.list(ast.get("select"));
        if (select == null || select.isEmpty()) {
            throw new AstError("select 가 비었습니다");
        }
        List<String> names = new ArrayList<>();
        List<Object[]> items = new ArrayList<>();  // [sql, info, name]
        boolean hasAgg = false;
        Map<String, String> fams = new LinkedHashMap<>();
        for (Object rawItem : select) {
            Map<String, Object> item = Dyn.map(rawItem);
            if (item == null || !item.containsKey("expr")) {
                throw new AstError("select 항목은 {\"expr\": 식, \"as\": \"이름\"} 입니다");
            }
            Map<String, Object> expr = Dyn.map(item.get("expr"));
            if (Dyn.onlyKey(expr, "col") && "*".equals(expr.get("col"))) {
                for (Map.Entry<String, Map<String, Integer>> e : scope.sources.entrySet()) {
                    for (String col : e.getValue().keySet()) {
                        if (names.contains(col)) {
                            throw new AstError("* 로 펼친 컬럼 이름이 겹칩니다 -> " + col + " (컬럼을 하나씩 적으십시오)");
                        }
                        names.add(col);
                        fams.put(col, scope.family(e.getKey(), col));
                        Info info = Info.of();
                        info.cols = true;
                        items.add(new Object[] {column(e.getKey(), col, scope), info, col});
                    }
                }
                continue;
            }
            String name = Py.text(item.get("as")).strip();
            if (name.isEmpty()) {
                throw new AstError("select 항목에 as(결과 이름)가 없습니다 -> " + Json.dumps(item.get("expr")));
            }
            if (names.contains(name)) {
                throw new AstError("select 이름이 겹칩니다 -> " + name);
            }
            names.add(name);
            fams.put(name, familyOf(item.get("expr"), scope));
            Part part = expr(item.get("expr"), scope, false);
            hasAgg |= part.info().agg;
            items.add(new Object[] {part.sql(), part.info(), name});
        }

        List<String> groupSql = new ArrayList<>();
        for (Object e : Dyn.items(ast.get("group_by"))) {
            Part part = expr(e, scope, false);
            if (part.info().agg || part.info().window) {
                throw new AstError("group_by 에 집계·윈도 함수를 쓸 수 없습니다");
            }
            groupSql.add(part.sql());
        }
        if (hasAgg || !groupSql.isEmpty()) {
            for (Object[] item : items) {
                Info info = (Info) item[1];
                if (!info.agg && !info.window && info.cols && !groupSql.contains((String) item[0])) {
                    throw new AstError("집계하지 않은 select 항목은 group_by 에 같은 식으로 있어야 합니다 -> " + item[2]);
                }
            }
        }

        String head = Py.truthy(ast.get("distinct")) ? "SELECT DISTINCT " : "SELECT ";
        List<String> columns = new ArrayList<>();
        for (Object[] item : items) {
            columns.add(item[0] + " AS " + d.q((String) item[2]));
        }
        List<String> parts = new ArrayList<>();
        parts.add(head + String.join(", ", columns));
        parts.add("FROM " + fromSql);
        String where = conditions(ast.get("where"), scope, false);
        if (!where.isEmpty()) {
            parts.add("WHERE " + where);
        }
        if (!groupSql.isEmpty()) {
            parts.add("GROUP BY " + String.join(", ", groupSql));
        }
        String having = conditions(ast.get("having"), scope, true);
        if (!having.isEmpty()) {
            if (!(hasAgg || !groupSql.isEmpty())) {
                throw new AstError("having 은 집계할 때만 씁니다");
            }
            parts.add("HAVING " + having);
        }
        String order = order(ast.get("order_by"), scope, names);
        if (!order.isEmpty()) {
            parts.add("ORDER BY " + order);
        }
        if (ast.get("limit") != null) {
            Object limit = ast.get("limit");
            if (!Py.isInt(limit) || ((Number) limit).longValue() < 1 || ((Number) limit).longValue() > MAX_LIMIT) {
                throw new AstError("limit 은 1~" + MAX_LIMIT + " 정수입니다");
            }
            parts.add(d.limit(((Number) limit).intValue()));
        }
        level = outerLevel;
        outputFamilies = fams;
        return new Query(String.join(" ", parts), names);
    }

    /** 하위 쿼리 하나 (from 이나 joins). 바깥에서는 하위 쿼리 select 의 이름을 컬럼으로 쓴다. */
    private String subquery(Object ast, String alias, Scope scope, int depth) {
        Query inner = query(ast, depth + 1);
        Map<String, Integer> columns = new LinkedHashMap<>();
        for (String n : inner.names()) {
            columns.put(n, 0);
        }
        scope.add(alias, columns, outputFamilies);
        return "(" + inner.sql() + ") " + dialect.q(alias);
    }

    /** 테이블 하나. 별칭(as)이 없으면 테이블 이름이 곧 별칭이라 컬럼을 '테이블.컬럼' 으로 가리킨다. */
    private String table(Object rawName, Scope scope, Object rawAlias) {
        if (rawName instanceof String name && ctes.containsKey(name)) {  // with 로 정의한 이름
            Cte cte = ctes.get(name);
            String alias = Py.text(rawAlias).strip().isEmpty() ? name : Py.text(rawAlias).strip();
            scope.add(alias, cte.columns(), cte.families());
            return alias.equals(name) ? dialect.q(name) : dialect.q(name) + " " + dialect.q(alias);
        }
        if (!(rawName instanceof String name) || !tables.containsKey(name)) {
            throw new AstError("없는 테이블입니다 -> " + rawName);
        }
        String alias = Py.text(rawAlias).strip().isEmpty() ? name : Py.text(rawAlias).strip();
        scope.add(alias, tables.get(name), families.get(name));
        scope.real.put(alias, name);
        String schema = schemas.get(name);
        // 다른 스키마의 테이블은 '스키마.테이블' 이름 그대로를 별칭으로 둔다 — 컬럼을 그 이름으로 가리킨다
        if (!schema.isEmpty() || !alias.equals(name)) {
            return dialect.table(name, schema) + " " + dialect.q(alias);
        }
        return dialect.q(name);
    }

    /**
     * 계층 하나. 시작 행에서 key, parent 를 따라간 행 전부와 depth(시작 = 0).
     *
     * 재귀 CTE 는 (key, parent, depth) 만 들고 다니고, 끝에 key 마다 가장 얕은 depth 로 원래 행을 붙인다 —
     * 같은 행에 여러 길로 닿아도 한 번만 나온다. MAX_TREE_DEPTH 에서 멈추므로 고리가 있어도 끝난다.
     */
    private String tree(Object rawSpec, Object rawAlias, Scope scope) {
        Map<String, Object> spec = Dyn.map(rawSpec);
        if (spec == null) {
            throw new AstError("tree 는 {\"table\", \"key\", \"parent\", \"start\", \"direction\"} 입니다");
        }
        TreeSet<String> unknown = new TreeSet<>(spec.keySet());
        unknown.removeAll(TREE_KEYS);
        if (!unknown.isEmpty()) {
            throw new AstError("tree 에 모르는 키입니다 -> " + String.join(", ", unknown) + " (쓸 수 있는 키: "
                    + String.join(", ", new TreeSet<>(TREE_KEYS)) + ")");
        }
        Object rawName = spec.get("table");
        if (!(rawName instanceof String name) || !tables.containsKey(name)) {
            throw new AstError("없는 테이블입니다 -> " + rawName);
        }
        Map<String, Integer> columns = tables.get(name);
        Map<String, String> fams = families.get(name);
        Object key = spec.get("key");
        Object parent = spec.get("parent");
        for (Object[] pair : new Object[][] {{"key", key}, {"parent", parent}}) {
            if (!(pair[1] instanceof String col) || !columns.containsKey(col)) {
                throw new AstError("tree 의 " + pair[0] + " 는 " + name + " 의 컬럼입니다 -> " + pair[1] + " (있는 것: "
                        + String.join(", ", columns.keySet()) + ")");
            }
        }
        String keyCol = (String) key;
        String parentCol = (String) parent;
        String direction = Py.text(spec.get("direction")).isEmpty() ? "down" : Py.text(spec.get("direction")).toLowerCase();
        if (!direction.equals("down") && !direction.equals("up")) {
            throw new AstError("tree 의 direction 은 down(자식 쪽) 이나 up(부모 쪽) 입니다");
        }
        List<Object> start = Dyn.list(spec.get("start"));
        if (start == null || start.isEmpty()) {
            throw new AstError("tree 의 start 는 시작 행을 고르는 조건 목록입니다 (예: 사용자의 그룹)");
        }
        String alias = Py.text(rawAlias).strip().isEmpty() ? name : Py.text(rawAlias).strip();
        String depthName = columns.containsKey("depth") ? "tree_depth" : "depth";

        Dialect d = dialect;
        Scope startScope = new Scope(d);
        startScope.add(name, columns, fams);
        startScope.real.put(name, name);
        String where = conditions(start, startScope, false);

        trees += 1;
        String cte = d.q("tree" + trees);
        String source = d.table(name, schemas.get(name));
        // 다음 단계: down 은 자식의 parent = 지난 key, up 은 부모의 key = 지난 parent
        String child;
        String prev;
        if (direction.equals("down")) {
            child = "c." + d.q(parentCol);
            prev = "t." + d.q("k");
        } else {
            child = "c." + d.q(keyCol);
            prev = "t." + d.q("p");
        }
        if (!java.util.Objects.equals(fams.get(parentCol), fams.get(keyCol))) {
            child = d.text(child);
            prev = d.text(prev);
        }
        List<String> listed = new ArrayList<>();
        for (String col : columns.keySet()) {
            listed.add("s." + d.q(col));
        }
        String sql = "(" + d.recursive() + " " + cte + " (" + d.q("k") + ", " + d.q("p") + ", " + d.q("depth") + ") AS ("
                + "SELECT " + d.q(keyCol) + ", " + d.q(parentCol) + ", 0 FROM " + source + " WHERE " + where + " "
                + "UNION ALL "
                + "SELECT c." + d.q(keyCol) + ", c." + d.q(parentCol) + ", t." + d.q("depth") + " + 1 FROM " + source + " c "
                + "JOIN " + cte + " t ON " + child + " = " + prev + " WHERE t." + d.q("depth") + " < " + MAX_TREE_DEPTH + ") "
                + "SELECT " + String.join(", ", listed) + ", m." + d.q("depth") + " AS " + d.q(depthName) + " FROM " + source + " s "
                + "JOIN (SELECT " + d.q("k") + ", MIN(" + d.q("depth") + ") AS " + d.q("depth") + " FROM " + cte
                + " GROUP BY " + d.q("k") + ") m "
                + "ON s." + d.q(keyCol) + " = m." + d.q("k") + ") " + d.q(alias);
        Map<String, Integer> outColumns = new LinkedHashMap<>(columns);
        outColumns.put(depthName, 0);
        Map<String, String> outFamilies = new LinkedHashMap<>(fams);
        outFamilies.put(depthName, "number");
        scope.add(alias, outColumns, outFamilies);
        scope.real.put(alias, name);  // 계산 필요 컬럼 검사와 '테이블.컬럼' 참조에 쓴다
        return sql;
    }

    /**
     * [계산 필요] 컬럼의 집계. 고른 지표가 그 컬럼에 쓴 집계 함수만 된다.
     * 지표가 쓰지 않은 컬럼은 MIN, MAX, COUNT (값을 보여 주거나 세기)만 된다 — 더하거나 평균내면 틀린다.
     */
    private void checkAggregate(String fn, List<Ref> refs) {
        if (allowed == null) {
            return;
        }
        for (Ref ref : refs) {
            Set<String> used = allowed.get(ref.column());
            if (used == null && (fn.equals("SUM") || fn.equals("AVG"))) {
                calcError(ref.table(), ref.column(), fn, "지표의 계산 식을 그대로 쓰십시오");
            }
            if (used != null && !used.contains(fn)) {
                calcError(ref.table(), ref.column(), fn, "지표처럼 " + String.join(", ", new TreeSet<>(used)) + " 로 계산하십시오");
            }
        }
    }

    /** [계산 필요] 컬럼의 집계끼리 계산(예: MAX - MIN = 하루 사용량)은 그 컬럼을 쓰는 지표를 골랐을 때만 된다 */
    private void checkArithmetic(String fn, List<Ref> refs) {
        if (allowed == null) {
            return;
        }
        for (Ref ref : refs) {
            if (!allowed.containsKey(ref.column())) {
                calcError(ref.table(), ref.column(), fn, "지표의 계산 식을 그대로 쓰십시오");
            }
        }
    }

    private static void calcError(String table, String column, String fn, String hint) {
        throw new CalcColumnError(
                table + "." + column + " 은 [계산 필요] 컬럼이라 지표에 없는 계산(" + fn + ")을 할 수 없습니다 — " + hint,
                table, column);
    }

    /** 컬럼 SQL. UTC 컬럼은 현지 시각으로 보정한다. */
    private String column(String alias, String col, Scope scope) {
        String raw = scope.raw(alias, col);
        int hours = scope.sources.get(alias).get(col);
        return hours != 0 ? dialect.addInterval(raw, hours, "hours") : raw;
    }

    private String order(Object rawItems, Scope scope, List<String> names) {
        List<String> out = new ArrayList<>();
        for (Object raw : Dyn.items(rawItems)) {
            Map<String, Object> item = Dyn.map(raw);
            if (item == null || !item.containsKey("expr")) {
                throw new AstError("order_by 항목은 {\"expr\": 식, \"dir\": \"asc|desc\"} 입니다");
            }
            Map<String, Object> target = Dyn.map(item.get("expr"));
            String sql;
            if (Dyn.onlyKey(target, "col") && names.contains(target.get("col"))) {
                sql = dialect.q((String) target.get("col"));  // select 이름으로 정렬
            } else {
                sql = expr(item.get("expr"), scope, false).sql();
            }
            String way = String.valueOf(item.containsKey("dir") ? item.get("dir") : "asc").toLowerCase();
            if (!way.equals("asc") && !way.equals("desc")) {
                throw new AstError("order_by dir 은 asc 나 desc 입니다");
            }
            out.add(sql + " " + way.toUpperCase());
        }
        return String.join(", ", out);
    }

    // ── 조건 ─────────────────────────────────────────────────────────────

    private String conditions(Object items, Scope scope, boolean allowAgg) {
        if (items == null) {
            return "";
        }
        List<Object> list = Dyn.list(items);
        if (list == null) {
            throw new AstError("where·having 은 조건 목록입니다");
        }
        List<String> out = new ArrayList<>();
        for (Object c : list) {
            out.add(condition(c, scope, allowAgg));
        }
        return String.join(" AND ", out);
    }

    private String condition(Object raw, Scope scope, boolean allowAgg) {
        Map<String, Object> c = Dyn.map(raw);
        if (c != null && (c.containsKey("or") || c.containsKey("and"))) {
            String key = c.containsKey("or") ? "or" : "and";
            List<Object> group = Dyn.list(c.get(key));
            if (group == null || group.isEmpty()) {
                throw new AstError(key + " 는 조건 목록입니다");
            }
            List<String> out = new ArrayList<>();
            for (Object x : group) {
                out.add(condition(x, scope, allowAgg));
            }
            String joined = String.join(" " + key.toUpperCase() + " ", out);
            return group.size() > 1 ? "(" + joined + ")" : joined;
        }
        if (c == null || !c.containsKey("expr") || !c.containsKey("op")) {
            throw new AstError("조건은 {\"expr\": 식, \"op\": \"=\", \"value\": 값} 입니다");
        }
        String op = String.valueOf(c.get("op")).strip().toLowerCase();
        if (!CONDITION_OPS.contains(op)) {
            throw new AstError("모르는 연산자입니다 -> " + op + " (쓸 수 있는 것: " + String.join(" ", CONDITION_OPS) + ")");
        }
        Object expr = c.get("expr");
        Object value = c.get("value");
        if ((op.equals("in") || op.equals("not_in")) && value instanceof Map<?, ?> vm && vm.containsKey("query")) {
            return inQuery(expr, op, vm.get("query"), scope);
        }

        String[] utc = utcColumn(expr, scope);
        if (utc != null && (COMPARE.containsKey(op) || op.equals("in") || op.equals("not_in") || op.equals("between"))) {
            Object shifted = utcValues(value, Integer.parseInt(utc[1]));
            if (shifted != null) {
                return compare(utc[0], op, shifted);
            }
        }

        Part left = expr(expr, scope, false);
        if (left.info().window || (left.info().agg && !allowAgg)) {
            throw new AstError("where 에는 집계·윈도 함수를 쓸 수 없습니다 (집계 조건은 having, 순위 조건은 하위 쿼리 바깥에서)");
        }
        if (op.equals("is_null") || op.equals("is_not_null")) {
            return left.sql() + " " + (op.equals("is_null") ? "IS NULL" : "IS NOT NULL");
        }
        if (op.equals("in") || op.equals("not_in") || op.equals("between")) {
            List<Object> values = Dyn.list(value);
            if (values == null || values.isEmpty() || (op.equals("between") && values.size() != 2)) {
                throw new AstError(op + " 의 value 는 값 목록입니다" + (op.equals("between") ? " (두 개)" : ""));
            }
            List<String> right = new ArrayList<>();
            for (Object v : values) {
                right.add(value(v, scope, allowAgg));
            }
            return compare(left.sql(), op, right);
        }
        if (op.equals("like") || op.equals("not_like")) {
            return left.sql() + " " + (op.equals("like") ? "LIKE" : "NOT LIKE") + " " + dialect.literal(value);
        }
        return compare(left.sql(), op, value(value, scope, allowAgg));
    }

    /** 컬럼 IN (하위 쿼리). 예: 사용자의 그룹에서 시작하는 계층. 타입 갈래가 다르면 둘 다 문자로 비교한다. */
    private String inQuery(Object expr, String op, Object ast, Scope scope) {
        Dialect d = dialect;
        Part left = expr(expr, scope, false);
        if (left.info().agg || left.info().window) {
            throw new AstError("in 하위 쿼리의 왼쪽에는 집계·윈도 함수를 쓸 수 없습니다");
        }
        Query inner = query(ast, level + 1);
        if (inner.names().size() != 1) {
            throw new AstError("in 의 하위 쿼리는 select 가 하나여야 합니다 -> " + String.join(", ", inner.names()));
        }
        String innerFamily = outputFamilies.get(inner.names().get(0));
        String leftFamily = familyOf(expr, scope);
        String leftSql = left.sql();
        String innerSql = inner.sql();
        if (Py.truthy(leftFamily) && Py.truthy(innerFamily) && !leftFamily.equals(innerFamily)) {
            leftSql = d.text(leftSql);
            innerSql = "SELECT " + d.text("x." + d.q(inner.names().get(0))) + " FROM (" + innerSql + ") x";
        }
        return leftSql + " " + (op.equals("in") ? "IN" : "NOT IN") + " (" + innerSql + ")";
    }

    /** 컬럼 하나인 식의 타입 갈래. 그 밖의 식이면 null */
    private String familyOf(Object expr, Scope scope) {
        Map<String, Object> e = Dyn.map(expr);
        if (Dyn.onlyKey(e, "col") && e.get("col") instanceof String col && !col.equals("*")) {
            Scope.Resolved r = scope.resolve(col);
            return scope.family(r.alias(), r.col());
        }
        return null;
    }

    @SuppressWarnings("unchecked")
    private String compare(String left, String op, Object right) {
        if (op.equals("in") || op.equals("not_in")) {
            return left + " " + (op.equals("in") ? "IN" : "NOT IN") + " (" + String.join(", ", (List<String>) right) + ")";
        }
        if (op.equals("between")) {
            List<String> r = (List<String>) right;
            return left + " BETWEEN " + r.get(0) + " AND " + r.get(1);
        }
        return left + " " + COMPARE.get(op) + " " + right;
    }

    private String value(Object value, Scope scope, boolean allowAgg) {
        if (value instanceof Map) {
            Part part = expr(value, scope, false);
            if (part.info().window || (part.info().agg && !allowAgg)) {
                throw new AstError("where 의 값에는 집계·윈도 함수를 쓸 수 없습니다");
            }
            return part.sql();
        }
        if (value instanceof List) {
            throw new AstError("값 목록은 in · not_in · between 에만 씁니다");
        }
        return dialect.literal(value);
    }

    /** 보정 대상 컬럼 하나만 있는 식이면 [저장된 컬럼 SQL, 시차] */
    private String[] utcColumn(Object expr, Scope scope) {
        Map<String, Object> e = Dyn.map(expr);
        if (Dyn.onlyKey(e, "col") && !"*".equals(e.get("col"))) {
            Scope.Resolved r = scope.resolve(e.get("col"));
            if (r.hours() != 0) {
                return new String[] {scope.raw(r.alias(), r.col()), String.valueOf(r.hours())};
            }
        }
        return null;
    }

    /** 현지 시각 상수(CURRENT_DATE 는 오늘 날짜로)를 UTC 로. 그 밖의 식이면 null (그때는 컬럼을 보정해 비교한다). */
    private Object utcValues(Object value, int hours) {
        List<Object> values = value instanceof List<?> ? Dyn.list(value) : java.util.Collections.singletonList(value);
        if (values.isEmpty()) {
            return null;
        }
        List<String> out = new ArrayList<>();
        for (Object v : values) {
            Map<String, Object> vm = Dyn.map(v);
            if (Dyn.onlyKey(vm, "str")) {
                v = vm.get("str");  // 값을 {"str": "…"} 식으로 적어도 상수다
            }
            LocalDateTime moment = v instanceof String s ? parseDatetime(s) : localDay(v, hours);
            if (moment == null) {
                return null;
            }
            out.add(dialect.addInterval(dialect.timestamp(moment), -hours, "hours"));
        }
        return value instanceof List ? out : out.get(0);
    }

    /**
     * CURRENT_DATE · DATE(CURRENT_DATE) · ADD_DAYS(그것, n) 이면 그 컬럼의 현지 날짜 0시, 아니면 null.
     * DB 의 CURRENT_DATE 는 DB 시간대 날짜라 현지 '오늘' 과 다를 수 있고, 컬럼을 감싸면 인덱스를 못 탄다.
     */
    private LocalDateTime localDay(Object value, int hours) {
        Map<String, Object> v = Dyn.map(value);
        if (v == null) {
            return null;
        }
        String fn = Dyn.text(v, "fn").toUpperCase();
        List<Object> args = Dyn.items(v.get("args"));
        if (fn.equals("CURRENT_DATE") && args.isEmpty()) {
            LocalDateTime now = utcNow != null ? utcNow : LocalDateTime.now(ZoneOffset.UTC);
            LocalDate local = now.plusHours(hours).toLocalDate();
            return local.atStartOfDay();
        }
        if (fn.equals("DATE") && args.size() == 1) {
            return localDay(args.get(0), hours);
        }
        if (fn.equals("ADD_DAYS") && args.size() == 2 && args.get(1) instanceof Map) {
            Object n = Dyn.map(args.get(1)).get("num");
            LocalDateTime day = localDay(args.get(0), hours);
            if (day == null || !Py.isInt(n)) {
                return null;
            }
            return day.plusDays(((Number) n).longValue());
        }
        return null;
    }

    // ── 식 → (SQL, {agg, window, cols}) ──────────────────────────────────

    private Part expr(Object raw, Scope scope, boolean inAgg) {
        Dialect d = dialect;
        Map<String, Object> e = Dyn.map(raw);
        if (e == null) {
            throw new AstError("식은 {\"col\"}, {\"num\"}, {\"str\"}, {\"fn\", \"args\"} 중 하나입니다 -> " + Json.dumps(raw));
        }
        if (e.containsKey("col")) {
            if ("*".equals(e.get("col"))) {
                throw new AstError("{\"col\": \"*\"} 는 select 항목으로만 씁니다 (건수는 {\"fn\": \"COUNT\", \"args\": []})");
            }
            Scope.Resolved r = scope.resolve(e.get("col"));
            String table = scope.real.get(r.alias());
            List<Ref> rawRefs = new ArrayList<>();
            if (table != null && calcColumns.getOrDefault(table, Set.of()).contains(r.col())) {
                rawRefs.add(new Ref(table, r.col()));
            }
            return new Part(column(r.alias(), r.col(), scope), Info.cols(rawRefs));
        }
        if (e.containsKey("num")) {
            if (!(e.get("num") instanceof Number n)) {
                throw new AstError("num 은 숫자입니다 -> " + Json.dumps(e.get("num")));
            }
            return new Part(Py.repr(n), Info.of());
        }
        if (e.containsKey("str")) {
            return new Part(d.literal(Py.str(e.get("str"))), Info.of());
        }
        String fn = Dyn.text(e, "fn").toUpperCase();
        Object rawArgs = e.get("args");
        if (fn.isEmpty()) {
            throw new AstError("식은 {\"col\"}, {\"num\"}, {\"str\"}, {\"fn\", \"args\"} 중 하나입니다 -> " + Json.dumps(raw));
        }
        if (Py.truthy(rawArgs) && !(rawArgs instanceof List)) {
            throw new AstError(fn + " 의 args 는 목록입니다");
        }
        List<Object> args = Dyn.items(rawArgs);

        if (AGGREGATES.contains(fn)) {
            if (inAgg) {
                throw new AstError("집계 안에 집계를 쓸 수 없습니다 -> " + fn + " (두 단계 집계는 from 에 하위 쿼리를 쓰십시오)");
            }
            if (fn.equals("COUNT") && args.isEmpty()) {
                Info info = Info.of();
                info.agg = true;
                return new Part("COUNT(*)", info);
            }
            if (args.size() != 1) {
                throw new AstError(fn + " 은 인자 하나입니다");
            }
            Part inner = expr(args.get(0), scope, true);
            if (inner.info().window) {
                throw new AstError("집계 안에 윈도 함수를 쓸 수 없습니다");
            }
            if (!inner.info().raw.isEmpty()) {
                checkAggregate(fn, inner.info().raw);
            }
            String body = fn.equals("COUNT_DISTINCT") ? "COUNT(DISTINCT " + inner.sql() + ")" : fn + "(" + inner.sql() + ")";
            Info info = inner.info().copy();
            info.agg = true;
            info.calc = new ArrayList<>(inner.info().raw);
            info.raw = new ArrayList<>();
            return new Part(body, info);
        }
        if (ARITHMETIC.containsKey(fn)) {
            if (args.size() < 2) {
                throw new AstError(fn + " 은 인자 둘 이상입니다");
            }
            List<Part> parts = subs(args, scope, inAgg);
            Info merged = merge(parts);
            if (!merged.calc.isEmpty()) {
                checkArithmetic(fn, merged.calc);
            }
            return new Part("(" + String.join(" " + ARITHMETIC.get(fn) + " ", sqls(parts)) + ")", merged);
        }
        if (fn.equals("DIV")) {
            if (args.size() != 2) {
                throw new AstError("DIV 는 인자 둘입니다");
            }
            List<Part> parts = subs(args, scope, inAgg);
            Info merged = merge(parts);
            if (!merged.calc.isEmpty()) {
                checkArithmetic(fn, merged.calc);
            }
            return new Part("(" + parts.get(0).sql() + " * 1.0 / NULLIF(" + parts.get(1).sql() + ", 0))", merged);
        }
        if (SCALARS.containsKey(fn)) {
            int[] range = SCALARS.get(fn);
            if (args.size() < range[0] || args.size() > range[1]) {
                throw new AstError(fn + " 은 인자 " + range[0] + "~" + range[1] + "개입니다");
            }
            List<Part> parts = subs(args, scope, inAgg);
            return new Part(fn + "(" + String.join(", ", sqls(parts)) + ")", merge(parts));
        }
        if (fn.equals("DATE")) {
            if (args.size() != 1) {
                throw new AstError("DATE 는 인자 하나입니다");
            }
            Part inner = expr(args.get(0), scope, inAgg);
            return new Part(d.date(inner.sql()), inner.info());
        }
        if (fn.equals("DATE_TRUNC")) {
            String unit = Py.text(e.get("unit")).toLowerCase();
            if (!TRUNC_UNITS.contains(unit) || args.size() != 1) {
                throw new AstError("DATE_TRUNC 는 {\"fn\": \"DATE_TRUNC\", \"unit\": \"" + String.join("|", TRUNC_UNITS)
                        + "\", \"args\": [식]} 입니다");
            }
            Part inner = expr(args.get(0), scope, inAgg);
            return new Part(d.trunc(unit, inner.sql()), inner.info());
        }
        if (INTERVAL_FUNCTIONS.containsKey(fn)) {
            if (args.size() != 2 || !(args.get(1) instanceof Map) || !Dyn.map(args.get(1)).containsKey("num")) {
                throw new AstError(fn + " 은 [식, {\"num\": 숫자}] 입니다");
            }
            Object n = Dyn.map(args.get(1)).get("num");
            if (!(n instanceof Number num)) {
                throw new AstError(fn + " 의 두 번째 값은 숫자입니다");
            }
            String[] utc = utcColumn(args.get(0), scope);
            if (fn.equals("ADD_HOURS") && utc != null && num.doubleValue() == -Integer.parseInt(utc[1])) {
                Info info = Info.of();
                info.cols = true;
                return new Part(utc[0], info);  // 보정을 되돌리면 저장된 UTC 값 그대로
            }
            Part inner = expr(args.get(0), scope, inAgg);
            return new Part(d.addInterval(inner.sql(), num.doubleValue(), INTERVAL_FUNCTIONS.get(fn)), inner.info());
        }
        if (fn.equals("CASE")) {
            return caseExpr(e, scope, inAgg);
        }
        if (fn.equals("NOW")) {
            return new Part("CURRENT_TIMESTAMP", Info.of());
        }
        if (fn.equals("CURRENT_DATE")) {
            return new Part(d.currentDate(), Info.of());
        }
        if (fn.equals("ROW_NUMBER")) {
            if (inAgg) {
                throw new AstError("집계 안에 윈도 함수를 쓸 수 없습니다");
            }
            List<Part> part = subs(Dyn.items(e.get("partition_by")), scope, inAgg);
            String orderSql = order(e.get("order_by"), scope, List.of());
            if (orderSql.isEmpty()) {
                throw new AstError("ROW_NUMBER 에는 order_by 가 필요합니다");
            }
            String over = (part.isEmpty() ? "" : "PARTITION BY " + String.join(", ", sqls(part)) + " ") + "ORDER BY " + orderSql;
            Info info = merge(part);
            info.window = true;
            return new Part("ROW_NUMBER() OVER (" + over + ")", info);
        }
        TreeSet<String> known = new TreeSet<>(AGGREGATES);
        known.addAll(ARITHMETIC.keySet());
        known.addAll(SCALARS.keySet());
        known.addAll(OTHER_FUNCTIONS);
        throw new AstError("모르는 함수입니다 -> " + fn + " (쓸 수 있는 것: " + String.join(", ", known) + ")");
    }

    private List<Part> subs(List<Object> items, Scope scope, boolean inside) {
        List<Part> out = new ArrayList<>();
        for (Object a : items) {
            out.add(expr(a, scope, inside));
        }
        return out;
    }

    private static List<String> sqls(List<Part> parts) {
        List<String> out = new ArrayList<>();
        for (Part p : parts) {
            out.add(p.sql());
        }
        return out;
    }

    private static Info merge(List<Part> parts) {
        Info merged = Info.of();
        for (Part p : parts) {
            Info info = p.info();
            merged.agg |= info.agg;
            merged.window |= info.window;
            merged.cols |= info.cols;
            for (Ref ref : info.raw) {
                if (!merged.raw.contains(ref)) {
                    merged.raw.add(ref);
                }
            }
            for (Ref ref : info.calc) {
                if (!merged.calc.contains(ref)) {
                    merged.calc.add(ref);
                }
            }
        }
        return merged;
    }

    /** {"fn": "CASE", "when": [{"if": [조건], "then": 식}], "else": 식} — 조건은 where 와 같은 모양 */
    private Part caseExpr(Map<String, Object> e, Scope scope, boolean inAgg) {
        List<Object> whens = Dyn.list(e.get("when"));
        if (whens == null || whens.isEmpty()) {
            throw new AstError("CASE 는 {\"fn\": \"CASE\", \"when\": [{\"if\": [조건], \"then\": 식}], \"else\": 식} 입니다");
        }
        List<String> sql = new ArrayList<>();
        List<Part> parts = new ArrayList<>();
        for (Object rawWhen : whens) {
            Map<String, Object> w = Dyn.map(rawWhen);
            if (w == null || !w.containsKey("if") || !w.containsKey("then")) {
                throw new AstError("CASE 의 when 항목은 {\"if\": [조건], \"then\": 식} 입니다");
            }
            List<Object> conds = w.get("if") instanceof List ? Dyn.list(w.get("if")) : new ArrayList<>(List.of(w.get("if")));
            if (conds.isEmpty()) {
                throw new AstError("CASE 의 if 가 비었습니다");
            }
            Info info = conditionsInfo(conds, scope);
            if (inAgg && info.agg) {
                throw new AstError("집계 안에 집계를 쓸 수 없습니다 -> CASE 조건 (두 단계 집계는 from 에 하위 쿼리를 쓰십시오)");
            }
            Part then = expr(w.get("then"), scope, inAgg);
            sql.add("WHEN " + conditions(conds, scope, true) + " THEN " + then.sql());
            parts.add(new Part("", info));
            parts.add(then);
        }
        if (e.get("else") != null) {
            Part other = expr(e.get("else"), scope, inAgg);
            sql.add("ELSE " + other.sql());
            parts.add(other);
        }
        return new Part("(CASE " + String.join(" ", sql) + " END)", merge(parts));
    }

    /** 조건에 쓰인 식들의 {agg, window, cols} */
    private Info conditionsInfo(List<Object> conds, Scope scope) {
        List<Part> parts = new ArrayList<>();
        for (Object raw : conds) {
            Map<String, Object> c = Dyn.map(raw);
            if (c != null && (c.containsKey("or") || c.containsKey("and"))) {
                Object group = c.containsKey("or") ? c.get("or") : c.get("and");
                parts.add(new Part("", conditionsInfo(group instanceof List ? Dyn.list(group) : new ArrayList<>(), scope)));
                continue;
            }
            if (c == null || !c.containsKey("expr")) {
                continue;  // 모양 오류는 condition 이 알린다
            }
            parts.add(expr(c.get("expr"), scope, false));
            List<Object> values = c.get("value") instanceof List ? Dyn.list(c.get("value"))
                    : java.util.Collections.singletonList(c.get("value"));
            for (Object v : values) {
                if (v instanceof Map) {
                    parts.add(expr(v, scope, false));
                }
            }
        }
        return parts.isEmpty() ? Info.of() : merge(parts);
    }

    // ── AST 가 쓰는 테이블 ─────────────────────────────────────────────────

    /**
     * AST 가 쓰는 실제 테이블 이름. 가장 바깥 from 의 테이블이 먼저 온다.
     *
     * with 이름은 테이블이 아니다 — 그 이름이 쓰인 자리에서 with 안의 테이블로 바꿔 센다.
     */
    public static List<String> astTables(Object ast) {
        LinkedHashSet<String> out = new LinkedHashSet<>();
        Map<String, Object> cteQueries = new LinkedHashMap<>();
        Map<String, Object> top = Dyn.map(ast);
        if (top != null && top.get("with") instanceof List) {
            for (Map<String, Object> item : Dyn.maps(top.get("with"))) {
                if (item.get("name") instanceof String name) {
                    cteQueries.put(name.strip(), item.get("query"));
                }
            }
        }
        new TableWalker(out, cteQueries).walk(ast);
        return new ArrayList<>(out);
    }

    private static final class TableWalker {
        private final LinkedHashSet<String> out;
        private final Map<String, Object> cteQueries;
        private final Set<String> opened = new HashSet<>();

        TableWalker(LinkedHashSet<String> out, Map<String, Object> cteQueries) {
            this.out = out;
            this.cteQueries = cteQueries;
        }

        void add(Object name) {
            if (name instanceof String s && cteQueries.containsKey(s)) {
                if (opened.add(s)) {  // 같은 with 를 여러 번 써도 한 번만 연다
                    walk(cteQueries.get(s));
                }
            } else if (name instanceof String s) {
                out.add(s);
            }
        }

        void walk(Object raw) {
            Map<String, Object> node = Dyn.map(raw);
            if (node == null) {
                return;
            }
            Object source = node.get("from");
            Map<String, Object> sourceMap = Dyn.map(source);
            if (source instanceof String) {
                add(source);
            } else if (sourceMap != null && sourceMap.get("table") instanceof String) {
                add(sourceMap.get("table"));
            } else if (sourceMap != null && sourceMap.get("tree") instanceof Map) {
                Map<String, Object> tree = Dyn.map(sourceMap.get("tree"));
                add(tree.get("table"));
                conditions(tree.get("start"));
            } else if (sourceMap != null) {
                walk(sourceMap.get("query"));
            }
            for (Object rawJoin : Dyn.items(node.get("joins"))) {
                Map<String, Object> j = Dyn.map(rawJoin);
                if (j == null) {
                    continue;
                }
                if (j.get("table") instanceof String) {
                    add(j.get("table"));
                } else if (j.get("tree") instanceof Map) {
                    Map<String, Object> tree = Dyn.map(j.get("tree"));
                    add(tree.get("table"));
                    conditions(tree.get("start"));
                } else if (j.get("table") instanceof Map) {
                    walk(Dyn.map(j.get("table")).get("query"));
                } else if (j.containsKey("query")) {
                    walk(j.get("query"));
                }
            }
            conditions(node.get("where"));
            conditions(node.get("having"));
        }

        /** 조건의 in (하위 쿼리) 가 쓰는 테이블 */
        void conditions(Object items) {
            List<Object> list = items instanceof Map ? java.util.Collections.singletonList(items) : Dyn.items(items);
            for (Object raw : list) {
                Map<String, Object> c = Dyn.map(raw);
                if (c == null) {
                    continue;
                }
                conditions(c.get("or"));
                conditions(c.get("and"));
                if (c.get("value") instanceof Map) {
                    walk(Dyn.map(c.get("value")).get("query"));
                }
            }
        }
    }
}
