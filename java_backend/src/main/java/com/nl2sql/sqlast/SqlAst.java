package com.nl2sql.sqlast;

import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import com.nl2sql.common.Dyn;
import com.nl2sql.common.Json;
import com.nl2sql.common.Py;
import com.nl2sql.compiler.Compiler;

import net.sf.jsqlparser.JSQLParserException;
import net.sf.jsqlparser.expression.AnalyticExpression;
import net.sf.jsqlparser.expression.CaseExpression;
import net.sf.jsqlparser.expression.CastExpression;
import net.sf.jsqlparser.expression.DateTimeLiteralExpression;
import net.sf.jsqlparser.expression.DateUnitExpression;
import net.sf.jsqlparser.expression.DoubleValue;
import net.sf.jsqlparser.expression.Expression;
import net.sf.jsqlparser.expression.Function;
import net.sf.jsqlparser.expression.IntervalExpression;
import net.sf.jsqlparser.expression.LongValue;
import net.sf.jsqlparser.expression.NotExpression;
import net.sf.jsqlparser.expression.Parenthesis;
import net.sf.jsqlparser.expression.SignedExpression;
import net.sf.jsqlparser.expression.StringValue;
import net.sf.jsqlparser.expression.TimeKeyExpression;
import net.sf.jsqlparser.expression.WhenClause;
import net.sf.jsqlparser.expression.operators.arithmetic.Addition;
import net.sf.jsqlparser.expression.operators.arithmetic.Division;
import net.sf.jsqlparser.expression.operators.arithmetic.Multiplication;
import net.sf.jsqlparser.expression.operators.arithmetic.Subtraction;
import net.sf.jsqlparser.expression.operators.conditional.AndExpression;
import net.sf.jsqlparser.expression.operators.conditional.OrExpression;
import net.sf.jsqlparser.expression.operators.relational.Between;
import net.sf.jsqlparser.expression.operators.relational.EqualsTo;
import net.sf.jsqlparser.expression.operators.relational.ExistsExpression;
import net.sf.jsqlparser.expression.operators.relational.ExpressionList;
import net.sf.jsqlparser.expression.operators.relational.GreaterThan;
import net.sf.jsqlparser.expression.operators.relational.GreaterThanEquals;
import net.sf.jsqlparser.expression.operators.relational.InExpression;
import net.sf.jsqlparser.expression.operators.relational.IsBooleanExpression;
import net.sf.jsqlparser.expression.operators.relational.IsNullExpression;
import net.sf.jsqlparser.expression.operators.relational.LikeExpression;
import net.sf.jsqlparser.expression.operators.relational.MinorThan;
import net.sf.jsqlparser.expression.operators.relational.MinorThanEquals;
import net.sf.jsqlparser.expression.operators.relational.NotEqualsTo;
import net.sf.jsqlparser.expression.operators.relational.ParenthesedExpressionList;
import net.sf.jsqlparser.parser.CCJSqlParserUtil;
import net.sf.jsqlparser.schema.Column;
import net.sf.jsqlparser.schema.Table;
import net.sf.jsqlparser.statement.Statement;
import net.sf.jsqlparser.statement.Statements;
import net.sf.jsqlparser.statement.select.AllColumns;
import net.sf.jsqlparser.statement.select.AllTableColumns;
import net.sf.jsqlparser.statement.select.FromItem;
import net.sf.jsqlparser.statement.select.Join;
import net.sf.jsqlparser.statement.select.OrderByElement;
import net.sf.jsqlparser.statement.select.ParenthesedSelect;
import net.sf.jsqlparser.statement.select.PlainSelect;
import net.sf.jsqlparser.statement.select.Select;
import net.sf.jsqlparser.statement.select.SelectItem;
import net.sf.jsqlparser.statement.select.SetOperationList;
import net.sf.jsqlparser.statement.select.WithItem;

/**
 * SQL → 조회 명세(AST). 지표를 SQL 로 등록할 때 쓴다 (sql_ast.py — 파이썬은 sqlglot, 여기는 JSqlParser).
 *
 * AST 가 표현하지 못하는 문법은 무엇 때문인지 밝히고 실패한다 (UNION · OFFSET · 서브쿼리 조건 …).
 * 바꾼 AST 를 다시 컴파일한 SQL 이 원본과 같은 결과를 내는지는 MetricService.verify 가 대상 DB 에서 확인한다.
 *
 * UTC 로 저장된 시각 컬럼(utc_offset)은 AST 에서 현지 시각이다 (Compiler). 그래서 원본 SQL 의 뜻을 그대로 옮긴다.
 *   - 컬럼 + INTERVAL '시차 hours'  → 컬럼 (현지 시각)
 *   - 보정 없이 쓴 컬럼             → ADD_HOURS(컬럼, -시차) (원본처럼 UTC 값) — 경고를 남긴다
 *   - 컬럼 > '2026-10-01' (UTC 상수) → 컬럼 > '2026-10-01 09:00:00' (현지 상수)
 */
public final class SqlAst {

    /** 변환 결과: AST 와 경고 */
    public record Parsed(Map<String, Object> ast, List<String> warnings) {
    }

    static final Map<String, String> AGGREGATES = Map.of("SUM", "SUM", "AVG", "AVG", "MIN", "MIN", "MAX", "MAX");
    static final Set<String> SCALARS = Set.of("COALESCE", "GREATEST", "LEAST");
    static final Map<String, String> FLIP = Map.of("=", "=", "!=", "!=", ">", "<", ">=", "<=", "<", ">", "<=", ">=");
    static final Map<String, String> INTERVAL_FN =
            Map.of("seconds", "ADD_SECONDS", "minutes", "ADD_MINUTES", "hours", "ADD_HOURS", "days", "ADD_DAYS");
    static final Map<String, String> TRUNC = new LinkedHashMap<>();
    static final Set<String> TEXT_TYPES = Set.of("varchar", "char", "text", "nvarchar", "nchar", "character varying",
            "character", "varchar2", "nvarchar2", "string");
    private static final DateTimeFormatter STAMP = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");

    static {
        for (String[] pair : new String[][] {{"hour", "hour"}, {"hours", "hour"}, {"hh24", "hour"}, {"day", "day"},
                {"dd", "day"}, {"week", "week"}, {"iw", "week"}, {"month", "month"}, {"mm", "month"},
                {"year", "year"}, {"yyyy", "year"}}) {
            TRUNC.put(pair[0], pair[1]);
        }
    }

    /** 상수가 아니다 (파이썬 _NOT_LITERAL) */
    static final Object NOT_LITERAL = new Object();

    private SqlAst() {
    }

    /** → AST 와 경고. 바꿀 수 없으면 IllegalArgumentException. */
    public static Parsed parseSql(String sql, Map<String, Object> contract) {
        String text = sql == null ? "" : sql.strip();
        while (text.endsWith(";")) {
            text = text.substring(0, text.length() - 1);
        }
        text = text.strip();
        if (text.isEmpty()) {
            throw new IllegalArgumentException("SQL 이 비어 있습니다");
        }
        List<Statement> trees;
        try {
            Statements parsed = CCJSqlParserUtil.parseStatements(text);
            trees = new ArrayList<>();
            for (Statement s : parsed) {
                if (s != null) {
                    trees.add(s);
                }
            }
        } catch (JSQLParserException error) {
            throw new IllegalArgumentException("SQL 을 읽을 수 없습니다 -> " + firstLine(error));
        }
        if (trees.size() != 1) {
            throw new IllegalArgumentException("SQL 문이 " + trees.size() + "개입니다 — 하나만 넣으십시오");
        }
        Statement tree = trees.get(0);
        if (!(tree instanceof PlainSelect select)) {
            throw new IllegalArgumentException("SELECT 문만 받습니다 (" + kindName(tree) + " 은 지원하지 않습니다)");
        }
        Parser parser = new Parser(contract);
        List<WithItem<?>> withItems = select.getWithItemsList();
        if (withItems != null && !withItems.isEmpty()) {
            boolean recursive = false;
            for (WithItem<?> cte : withItems) {
                recursive |= cte.isRecursive();
            }
            for (WithItem<?> cte : withItems) {
                String alias = unquote(cte.getAliasName());
                Select body = cte.getSelect() == null ? null : cte.getSelect().getSelect();
                parser.ctes.put(alias, body);
                if (recursive && body instanceof SetOperationList) {
                    parser.recursive.put(alias, cte);  // 계층 — AST 의 tree 로 옮긴다
                }
            }
            // 두 번 이상 쓰는 WITH 는 AST 의 with 로 둔다 — 펼치면 같은 집계를 두 번 한다
            Map<String, Integer> counts = new LinkedHashMap<>();
            for (Table table : findTables(select)) {
                String name = unquote(table.getName());
                if (blank(table.getSchemaName()) && parser.ctes.containsKey(name) && !parser.recursive.containsKey(name)) {
                    counts.merge(name, 1, Integer::sum);
                }
            }
            for (Map.Entry<String, Integer> e : counts.entrySet()) {
                if (e.getValue() > 1) {
                    parser.shared.add(e.getKey());
                }
            }
        }
        Map<String, Object> ast = parser.select(select);
        if (!parser.withs.isEmpty()) {
            Map<String, Object> withAst = new LinkedHashMap<>();
            withAst.put("with", parser.withs);
            withAst.putAll(ast);
            ast = withAst;
        }
        return new Parsed(ast, new ArrayList<>(new LinkedHashSet<>(parser.warnings)));
    }

    /**
     * GROUP BY · ORDER BY 의 순번 참조 (GROUP BY 1, 2). 지표 SQL 은 질문마다 LLM 이 읽는다 — 순번은 SELECT 목록과
     * 맞춰 봐야 해서 헷갈리고, 조건을 바꾸다 SELECT 순서가 바뀌면 뜻이 달라진다. 읽지 못하면 빈 목록 (문법 오류는 AST 변환이 알린다).
     */
    public static List<String> positionalRefs(String sql) {
        List<String> found = new ArrayList<>();
        Statements parsed;
        try {
            parsed = CCJSqlParserUtil.parseStatements(sql);
        } catch (JSQLParserException error) {
            return found;
        }
        for (Statement tree : parsed) {
            for (Object node : findAll(tree)) {
                if (node instanceof PlainSelect select) {
                    if (select.getGroupBy() != null && !found.contains("GROUP BY")) {
                        for (Object e : select.getGroupBy().getGroupByExpressionList()) {
                            if (isNumberLiteral(e)) {
                                found.add("GROUP BY");
                                break;
                            }
                        }
                    }
                }
                if (node instanceof Select select && select.getOrderByElements() != null && !found.contains("ORDER BY")) {
                    for (OrderByElement o : select.getOrderByElements()) {
                        if (isNumberLiteral(o.getExpression())) {
                            found.add("ORDER BY");
                            break;
                        }
                    }
                }
            }
        }
        List<String> ordered = new ArrayList<>();
        for (String clause : List.of("GROUP BY", "ORDER BY")) {
            if (found.contains(clause)) {
                ordered.add(clause);
            }
        }
        return ordered;
    }

    // ── 범위 ──────────────────────────────────────────────────────────────

    /** FROM 소스 하나: AST 이름, {컬럼: 시차}, 실제 테이블인가 */
    static final class Source {
        final String name;
        final Map<String, Integer> columns;
        final boolean table;

        Source(String name, Map<String, Integer> columns, boolean table) {
            this.name = name;
            this.columns = columns;
            this.table = table;
        }
    }

    static final class Scope {
        final Map<String, Source> sources = new LinkedHashMap<>();  // SQL 별칭 → 소스

        Map.Entry<String, Source> owner(Column column) {
            String table = columnTable(column);
            String name = unquote(column.getColumnName());
            if (!table.isEmpty()) {
                for (Map.Entry<String, Source> e : sources.entrySet()) {
                    if (e.getKey().equalsIgnoreCase(table)) {
                        return e;
                    }
                }
                throw new IllegalArgumentException("FROM 에 없는 테이블·별칭입니다 -> " + table);
            }
            return ownerOf(name);
        }

        Map.Entry<String, Source> ownerOf(String name) {
            List<Map.Entry<String, Source>> owners = new ArrayList<>();
            for (Map.Entry<String, Source> e : sources.entrySet()) {
                if (find(e.getValue().columns, name) != null) {
                    owners.add(e);
                }
            }
            if (owners.isEmpty()) {
                throw new IllegalArgumentException("없는 컬럼입니다 -> " + name);
            }
            if (owners.size() > 1) {
                throw new IllegalArgumentException("여러 테이블에 있는 컬럼입니다 -> " + name + " (테이블을 붙여 적으십시오)");
            }
            return owners.get(0);
        }

        Map.Entry<String, Source> ownerByTable(String table) {
            for (Map.Entry<String, Source> e : sources.entrySet()) {
                if (e.getKey().equalsIgnoreCase(table)) {
                    return e;
                }
            }
            throw new IllegalArgumentException("FROM 에 없는 테이블·별칭입니다 -> " + table);
        }

        /** 어느 FROM 소스에든 있는 컬럼인가 */
        boolean knows(String name) {
            for (Source src : sources.values()) {
                if (find(src.columns, name) != null) {
                    return true;
                }
            }
            return false;
        }
    }

    /** 컬럼 이름을 실제 이름으로 (대소문자 무시) */
    static String find(Map<String, Integer> columns, String name) {
        if (columns.containsKey(name)) {
            return name;
        }
        for (String c : columns.keySet()) {
            if (c.equalsIgnoreCase(name)) {
                return c;
            }
        }
        return null;
    }

    /** 하위 쿼리 결과 컬럼 → 시차 0 (UTC 정보는 하위 쿼리 밖으로 나오지 않는다) */
    static Map<String, Integer> subqueryColumns(Map<String, Object> inner) {
        Map<String, Integer> columns = new LinkedHashMap<>();
        for (Map<String, Object> s : Dyn.maps(inner.get("select"))) {
            columns.put((String) s.get("as"), 0);
        }
        return columns;
    }

    // ── 변환기 ─────────────────────────────────────────────────────────────

    static final class Parser {
        final String driver;
        final Map<String, Object[]> tables = new LinkedHashMap<>();  // 소문자 이름 → [이름, {컬럼: 시차}]
        final Map<String, Select> ctes = new LinkedHashMap<>();
        final Set<String> shared = new HashSet<>();  // AST 의 with 로 둘 WITH 이름 (JOIN 에 쓰거나 두 번 이상 쓴다)
        final List<Map<String, Object>> withs = new ArrayList<>();  // [{"name", "query"}] — 먼저 필요한 것이 앞에 온다
        final Map<String, Map<String, Integer>> withColumns = new LinkedHashMap<>();
        final Set<String> opening = new HashSet<>();
        final Map<String, WithItem<?>> recursive = new LinkedHashMap<>();  // 맨 바깥 WITH RECURSIVE 이름 → CTE
        final List<String> warnings = new ArrayList<>();

        Parser(Map<String, Object> contract) {
            driver = Dyn.text(contract, "driver").isEmpty() ? "postgresql" : Dyn.text(contract, "driver");
            for (Map<String, Object> t : Dyn.maps(contract.get("tables"))) {
                Map<String, Integer> cols = new LinkedHashMap<>();
                for (Map<String, Object> c : Dyn.maps(t.get("columns"))) {
                    int hours = Dyn.integer(c, "utc_offset");
                    if (hours != 0 && !"datetime".equals(Compiler.temporalKind(Dyn.text(c, "type"), driver))) {
                        hours = 0;
                    }
                    cols.put((String) c.get("name"), hours);
                }
                String name = (String) t.get("name");
                tables.put(name.toLowerCase(), new Object[] {name, cols});
            }
        }

        // ── SELECT 한 단계 ───────────────────────────────────────────────

        Map<String, Object> select(Object raw) {
            Object query = raw instanceof ParenthesedSelect ps ? ps.getSelect() : raw;
            if (!(query instanceof PlainSelect select)) {
                throw new IllegalArgumentException("하위 쿼리는 SELECT 만 받습니다 (" + kindName(query) + " 은 지원하지 않습니다)");
            }
            if (select.getOffset() != null || (select.getLimit() != null && select.getLimit().getOffset() != null)) {
                throw new IllegalArgumentException("OFFSET 은(는) 지원하지 않습니다");
            }
            if (select.getQualify() != null) {
                throw new IllegalArgumentException("QUALIFY 은(는) 지원하지 않습니다");
            }
            if (select.getWindowDefinitions() != null && !select.getWindowDefinitions().isEmpty()) {
                throw new IllegalArgumentException("WINDOW 절 은(는) 지원하지 않습니다");
            }
            if (select.getDistinct() != null && select.getDistinct().getOnSelectItems() != null
                    && !select.getDistinct().getOnSelectItems().isEmpty()) {
                throw new IllegalArgumentException("DISTINCT ON 은 지원하지 않습니다 — ROW_NUMBER() OVER (...) 로 바꿔 적으십시오");
            }

            Scope scope = new Scope();
            Map<String, Object> ast = new LinkedHashMap<>();
            if (select.getFromItem() == null) {
                throw new IllegalArgumentException("FROM 이 없습니다");
            }
            ast.put("from", source(select.getFromItem(), scope, true));

            List<Object> joins = new ArrayList<>();
            if (select.getJoins() != null) {
                for (Join join : select.getJoins()) {
                    joins.add(join(join, scope));
                }
            }
            if (!joins.isEmpty()) {
                ast.put("joins", joins);
            }

            List<Map<String, Object>> items = new ArrayList<>();
            Map<String, Expression> aliases = new LinkedHashMap<>();
            int i = 0;
            for (SelectItem<?> item : select.getSelectItems()) {
                i += 1;
                Expression body = ident(item.getExpression());
                if (body instanceof AllColumns || body instanceof AllTableColumns) {
                    items.addAll(star(body, scope));
                    continue;
                }
                String name = item.getAlias() != null ? unquote(item.getAlias().getName())
                        : (body instanceof Column c ? unquote(c.getColumnName()) : "");
                if (name.isEmpty()) {
                    name = "col" + i;
                }
                items.add(Dyn.of("expr", node(body, scope, null), "as", name));
                aliases.put(name.toLowerCase(), body);
            }
            List<String> names = new ArrayList<>();
            Set<String> seen = new HashSet<>();
            for (Map<String, Object> s : items) {
                String as = (String) s.get("as");
                if (!seen.add(as.toLowerCase())) {
                    throw new IllegalArgumentException("SELECT 결과 이름이 겹칩니다 — 별칭(AS)을 붙여 주십시오");
                }
                names.add(as);
            }
            ast.put("select", items);
            if (select.getDistinct() != null) {
                ast.put("distinct", true);
            }

            if (select.getWhere() != null) {
                ast.put("where", conditions(select.getWhere(), scope, null));
            }

            if (select.getGroupBy() != null) {
                List<Object> groups = new ArrayList<>();
                for (Object g : select.getGroupBy().getGroupByExpressionList()) {
                    groups.add(groupItem((Expression) g, select, scope, aliases));
                }
                ast.put("group_by", groups);
            }

            if (select.getHaving() != null) {
                ast.put("having", conditions(select.getHaving(), scope, aliases));
            }

            if (select.getOrderByElements() != null) {
                List<Object> orders = new ArrayList<>();
                for (OrderByElement o : select.getOrderByElements()) {
                    orders.add(orderItem(o, scope, names));
                }
                ast.put("order_by", orders);
            }

            Expression limit = null;
            if (select.getLimit() != null) {
                limit = select.getLimit().getRowCount();
                if (!(limit instanceof LongValue)) {
                    throw new IllegalArgumentException("LIMIT 은 숫자만 받습니다 -> " + sqlOf(select.getLimit()));
                }
            } else if (select.getFetch() != null) {
                limit = select.getFetch().getExpression();
                if (!(limit instanceof LongValue)) {
                    throw new IllegalArgumentException("LIMIT 은 숫자만 받습니다 -> " + sqlOf(select.getFetch()));
                }
            }
            if (limit != null) {
                ast.put("limit", ((LongValue) limit).getValue());
            }
            return ast;
        }

        Object source(FromItem node, Scope scope, boolean first) {
            if (node instanceof Table table) {  // 스키마 이름(public.bms_log)은 떼고 테이블 이름으로 찾는다
                String name = unquote(table.getName());
                String alias = table.getAlias() != null ? unquote(table.getAlias().getName()) : "";
                boolean noDb = blank(table.getSchemaName());
                if (recursive.containsKey(name) && noDb) {
                    return treeSource(recursive.get(name), alias.isEmpty() ? name : alias, scope, true, null);
                }
                if (ctes.containsKey(name) && noDb) {
                    String as = alias.isEmpty() ? name : alias;
                    if (first && !shared.contains(name)) {  // 한 번만 쓰면 그 자리의 하위 쿼리로 펼친다
                        Map<String, Object> inner = select(ctes.get(name));
                        scope.sources.put(as, new Source(as, subqueryColumns(inner), false));
                        return Dyn.of("query", inner, "as", as);
                    }
                    scope.sources.put(as, new Source(as, with(name), false));
                    return as.equals(name) ? name : Dyn.of("table", name, "as", as);
                }
                Object[] found = lookup(table);
                String real = (String) found[0];
                @SuppressWarnings("unchecked")
                Map<String, Integer> columns = (Map<String, Integer>) found[1];
                // 별칭이 없으면 SQL 처럼 스키마를 뗀 테이블 이름으로 가리킨다 (sales.orders → orders.amount)
                String as = !alias.isEmpty() ? alias : (real.contains(".") ? name : real);
                for (Source s : scope.sources.values()) {
                    if (s.name.equals(real)) {
                        throw new IllegalArgumentException("같은 테이블을 두 번 쓸 수 없습니다 (셀프 조인) -> " + real);
                    }
                }
                scope.sources.put(as, new Source(real, columns, true));
                return real;
            }
            if (node instanceof ParenthesedSelect sub) {
                String alias = sub.getAlias() != null ? unquote(sub.getAlias().getName()) : "";
                if (!first && alias.isEmpty()) {
                    throw new IllegalArgumentException("JOIN 하는 하위 쿼리에는 별칭이 있어야 합니다 (예: JOIN (…) top ON …)");
                }
                String as = alias.isEmpty() ? "sub" : alias;
                Select inner = sub.getSelect();
                if (inner instanceof PlainSelect ps && ps.getWithItemsList() != null) {
                    boolean recursiveWith = false;
                    for (WithItem<?> w : ps.getWithItemsList()) {
                        recursiveWith |= w.isRecursive();
                    }
                    if (recursiveWith) {
                        return wrappedTree(ps, as, scope);
                    }
                }
                Map<String, Object> innerAst = select(inner);
                scope.sources.put(as, new Source(as, subqueryColumns(innerAst), false));
                return Dyn.of("query", innerAst, "as", as);
            }
            throw new IllegalArgumentException("FROM 에 쓸 수 없는 것입니다 -> " + sqlOf(node));
        }

        /** SQL 의 테이블 → [메타데이터 이름, {컬럼: 시차}] */
        Object[] lookup(Table node) {
            String name = unquote(node.getName());
            String db = blank(node.getSchemaName()) ? "" : unquote(node.getSchemaName());
            String written = db.isEmpty() ? name : db + "." + name;
            Object[] found = null;
            if (!db.isEmpty()) {  // 기본 스키마가 아닌 테이블은 '스키마.테이블' 이름으로 등록돼 있다
                found = tables.get(written.toLowerCase());
            }
            if (found == null) {
                found = tables.get(name.toLowerCase());
            }
            if (found == null) {
                throw new IllegalArgumentException("메타데이터에 없는 테이블입니다 -> " + written + " (동기화했는지 확인하십시오)");
            }
            return found;
        }

        // ── 계층 (재귀 WITH → tree) ────────────────────────────────────

        /** (WITH RECURSIVE t AS (…) SELECT 테이블 컬럼 … FROM 테이블 JOIN t …) 별칭 — 질문 처리가 만든 계층 SQL 모양 */
        Map<String, Object> wrappedTree(PlainSelect node, String alias, Scope scope) {
            List<WithItem<?>> found = new ArrayList<>();
            for (WithItem<?> cte : node.getWithItemsList()) {
                if (cte.getSelect() != null && cte.getSelect().getSelect() instanceof SetOperationList) {
                    found.add(cte);
                }
            }
            if (found.size() != 1) {
                throw new IllegalArgumentException("하위 쿼리 " + alias + " 의 재귀 WITH 는 계층 하나만 받습니다");
            }
            Map<String, Object> spec = treeSpec(found.get(0));
            FromItem bodyFrom = node.getFromItem();
            if (!(bodyFrom instanceof Table t) || !lookup(t)[0].equals(spec.get("table"))) {
                throw new IllegalArgumentException("하위 쿼리 " + alias + " 는 재귀 WITH 결과를 " + spec.get("table")
                        + " 에 붙인 모양이어야 합니다 (WITH RECURSIVE 를 맨 앞에 두고 그 이름을 FROM 에 쓰는 모양도 됩니다)");
            }
            return treeSource(found.get(0), alias, scope, false, spec);
        }

        /** 계층을 FROM 소스로. 컬럼은 계층 테이블의 컬럼 + depth 다. */
        @SuppressWarnings("unchecked")
        Map<String, Object> treeSource(WithItem<?> cte, String alias, Scope scope, boolean ownColumns,
                Map<String, Object> given) {
            Map<String, Object> spec = given != null ? given : treeSpec(cte);
            String table = (String) spec.get("table");
            Map<String, Integer> columns = new LinkedHashMap<>((Map<String, Integer>) tables.get(table.toLowerCase())[1]);
            columns.put(columns.containsKey("depth") ? "tree_depth" : "depth", 0);
            if (ownColumns) {  // WITH 이름을 그대로 쓴다 — 그 컬럼이 테이블 컬럼 이름이어야 tree 로 옮길 수 있다
                for (String name : cteNames(cte)) {
                    if (find(columns, name) == null) {
                        throw new IllegalArgumentException("재귀 WITH " + unquote(cte.getAliasName()) + " 의 컬럼은 " + table
                                + " 의 컬럼 이름 그대로 적어야 합니다 -> " + name + " (단계 수는 depth)");
                    }
                }
            }
            scope.sources.put(alias, new Source(alias, columns, false));
            return Dyn.of("tree", spec, "as", alias);
        }

        /**
         * 재귀 WITH → {"table", "key", "parent", "start", "direction"}
         *
         * 시작 SELECT: 테이블 하나에서 키 컬럼(첫 항목)을 WHERE 로 고른다.
         * 다음 단계 SELECT: 같은 테이블을 WITH 이름과 조인한다 — 테이블.parent = WITH.키 이면 아래로(down),
         * 테이블.키 = WITH.parent 이면 위로(up). 단계 수 제한(WHERE depth < n)은 컴파일러가 둔다.
         */
        @SuppressWarnings("unchecked")
        Map<String, Object> treeSpec(WithItem<?> cte) {
            String name = unquote(cte.getAliasName());
            String shape = "재귀 WITH " + name + " 은 '시작 SELECT UNION ALL 다음 단계 SELECT' 모양이어야 합니다";
            Select body = cte.getSelect() == null ? null : cte.getSelect().getSelect();
            if (!(body instanceof SetOperationList union) || union.getSelects().size() != 2
                    || !(union.getSelects().get(0) instanceof PlainSelect anchor)
                    || !(union.getSelects().get(1) instanceof PlainSelect step)) {
                throw new IllegalArgumentException(shape);
            }
            if (!(anchor.getFromItem() instanceof Table anchorTable)
                    || (anchor.getJoins() != null && !anchor.getJoins().isEmpty())) {
                throw new IllegalArgumentException("재귀 WITH " + name + " 의 시작 SELECT 는 테이블 하나에서 WHERE 로 고릅니다 "
                        + "(다른 테이블의 값은 WHERE 키 IN (SELECT …) 로 적으십시오)");
            }
            Object[] found = lookup(anchorTable);
            String real = (String) found[0];
            Map<String, Integer> columns = (Map<String, Integer>) found[1];
            List<String> anchorCols = new ArrayList<>();
            for (SelectItem<?> e : anchor.getSelectItems()) {
                Expression col = stripTextCast(e.getExpression());
                anchorCols.add(col instanceof Column c ? find(columns, unquote(c.getColumnName())) : null);
            }
            String key = anchorCols.isEmpty() ? null : anchorCols.get(0);
            if (key == null) {
                throw new IllegalArgumentException("재귀 WITH " + name + " 의 시작 SELECT 첫 항목은 " + real + " 의 키 컬럼이어야 합니다");
            }

            List<Join> joins = step.getJoins() == null ? List.of() : step.getJoins();
            if (!(step.getFromItem() instanceof Table stepTable) || !lookup(stepTable)[0].equals(real) || joins.size() != 1) {
                throw new IllegalArgumentException("재귀 WITH " + name + " 의 다음 단계 SELECT 는 " + real + " 를 " + name
                        + " 과 조인한 모양이어야 합니다");
            }
            Join join = joins.get(0);
            if (!(join.getFromItem() instanceof Table joined) || !unquote(joined.getName()).equals(name)) {
                throw new IllegalArgumentException("재귀 WITH " + name + " 의 다음 단계 SELECT 는 " + real + " 를 " + name
                        + " 과 조인한 모양이어야 합니다");
            }
            String cteAlias = joined.getAlias() != null ? unquote(joined.getAlias().getName()) : name;
            Expression on = onExpression(join);
            if (!(on instanceof EqualsTo eq)) {
                throw new IllegalArgumentException("재귀 WITH " + name + " 의 조인 조건은 컬럼 = 컬럼 하나입니다");
            }
            Column own = null;
            Column prev = null;
            for (Expression side : List.of(stripTextCast(eq.getLeftExpression()), stripTextCast(eq.getRightExpression()))) {
                if (!(side instanceof Column c)) {
                    throw new IllegalArgumentException("재귀 WITH " + name + " 의 조인 조건은 컬럼 = 컬럼 하나입니다");
                }
                String t = columnTable(c);
                if (t.equals(cteAlias) || t.equals(name)) {
                    prev = c;
                } else {
                    own = c;
                }
            }
            if (own == null || prev == null) {
                throw new IllegalArgumentException("재귀 WITH " + name + " 의 조인 조건은 " + real + " 컬럼 = " + name + " 컬럼 이어야 합니다");
            }
            String ownCol = find(columns, unquote(own.getColumnName()));
            List<String> names = cteNames(cte);
            String prevCol = null;
            String prevName = unquote(prev.getColumnName());
            for (int i = 0; i < names.size(); i++) {
                if (names.get(i).equalsIgnoreCase(prevName) && i < anchorCols.size()) {
                    prevCol = anchorCols.get(i);
                }
            }
            if (ownCol == null || prevCol == null) {
                throw new IllegalArgumentException("재귀 WITH " + name + " 의 조인 컬럼을 " + real + " 의 컬럼으로 읽지 못했습니다 -> "
                        + sqlOf(on));
            }
            String direction;
            String parent;
            if (prevCol.equals(key) && !ownCol.equals(key)) {
                direction = "down";
                parent = ownCol;
            } else if (ownCol.equals(key) && !prevCol.equals(key)) {
                direction = "up";
                parent = prevCol;
            } else {
                throw new IllegalArgumentException("재귀 WITH " + name + " 의 조인은 부모 = 키 모양이어야 합니다 -> " + sqlOf(on));
            }

            if (anchor.getWhere() == null) {
                throw new IllegalArgumentException("재귀 WITH " + name + " 의 시작 SELECT 에 WHERE(시작 행 조건)가 없습니다");
            }
            Scope startScope = new Scope();
            String startAlias = anchorTable.getAlias() != null ? unquote(anchorTable.getAlias().getName())
                    : (real.contains(".") ? unquote(anchorTable.getName()) : real);
            startScope.sources.put(startAlias, new Source(real, columns, true));
            List<Object> start = conditions(anchor.getWhere(), startScope, null);
            return Dyn.of("table", real, "key", key, "parent", parent, "start", start, "direction", direction);
        }

        Map<String, Object> join(Join join, Scope scope) {
            String how;
            if (join.isLeft() && !join.isNatural() && !join.isSemi()) {
                how = "left";
            } else if (!join.isLeft() && !join.isRight() && !join.isFull() && !join.isCross() && !join.isNatural()
                    && !join.isOuter() && !join.isSemi() && !join.isApply()) {
                how = "inner";
            } else {
                throw new IllegalArgumentException(joinLabel(join) + " JOIN 은 지원하지 않습니다 (INNER · LEFT 만)");
            }
            if (join.getUsingColumns() != null && !join.getUsingColumns().isEmpty()) {
                throw new IllegalArgumentException("JOIN … USING 은 지원하지 않습니다 — ON a.x = b.x 로 적으십시오");
            }
            Object table = source(join.getFromItem(), scope, false);
            String alias = null;
            Map<String, Object> tableMap = Dyn.map(table);
            if (tableMap != null && tableMap.containsKey("table")) {  // 별칭을 붙인 WITH 이름
                table = tableMap.get("table");
                alias = (String) tableMap.get("as");
            }
            Expression on = onExpression(join);
            if (on == null) {
                throw new IllegalArgumentException((table instanceof String ? table : Json.dumps(table)) + " JOIN 에 ON 이 없습니다");
            }
            List<Object> pairs = new ArrayList<>();
            for (Expression part : split(on, AndExpression.class)) {
                Expression left = part instanceof EqualsTo eq ? stripTextCast(eq.getLeftExpression()) : null;
                Expression right = part instanceof EqualsTo eq ? stripTextCast(eq.getRightExpression()) : null;
                if (!(left instanceof Column lc && right instanceof Column rc)) {
                    throw new IllegalArgumentException("JOIN 조건은 컬럼 = 컬럼 만 받습니다 -> " + sqlOf(part));
                }
                // 타입이 다른 컬럼끼리의 CAST(… AS VARCHAR) 는 벗긴다 — 컴파일러가 다시 맞춘다
                pairs.add(new ArrayList<>(List.of(qualified(lc, scope), qualified(rc, scope))));
            }
            Map<String, Object> treeMap = Dyn.map(table);
            if (treeMap != null && treeMap.containsKey("tree")) {  // 계층은 {"tree", "as"} 로 둔다
                return Dyn.of("tree", treeMap.get("tree"), "as", treeMap.get("as"), "type", how, "on", pairs);
            }
            Map<String, Object> out = Dyn.of("table", table, "type", how, "on", pairs);
            if (alias != null && !alias.isEmpty()) {
                out.put("as", alias);
            }
            return out;
        }

        /** WITH 하나를 AST 의 with 로 옮기고 그 결과 컬럼을 돌려준다 (한 번만 옮긴다) */
        Map<String, Integer> with(String name) {
            if (!withColumns.containsKey(name)) {
                if (opening.contains(name)) {
                    throw new IllegalArgumentException("재귀 WITH 는 지원하지 않습니다 -> " + name + " (계층은 질문으로 물으십시오)");
                }
                opening.add(name);
                Map<String, Object> inner = select(ctes.get(name));
                opening.remove(name);
                withs.add(Dyn.of("name", name, "query", inner));
                withColumns.put(name, subqueryColumns(inner));
            }
            return withColumns.get(name);
        }

        String qualified(Column column, Scope scope) {
            Source src = scope.owner(column).getValue();
            return src.name + "." + find(src.columns, unquote(column.getColumnName()));
        }

        /** SELECT * 를 컬럼마다 펼친다. UTC 컬럼은 원본처럼 저장된 값으로 (ADD_HOURS -시차). */
        List<Map<String, Object>> star(Expression body, Scope scope) {
            Map<String, Source> sources;
            if (body instanceof AllTableColumns tc && tc.getTable() != null && !blank(tc.getTable().getName())) {
                Map.Entry<String, Source> owner = scope.ownerByTable(unquote(tc.getTable().getName()));
                sources = Map.of(owner.getKey(), owner.getValue());
            } else {
                sources = scope.sources;
            }
            List<Map<String, Object>> out = new ArrayList<>();
            Set<String> seen = new HashSet<>();
            for (Source src : sources.values()) {
                for (Map.Entry<String, Integer> e : src.columns.entrySet()) {
                    String col = e.getKey();
                    if (!seen.add(col.toLowerCase())) {
                        throw new IllegalArgumentException("* 로 펼친 컬럼 이름이 겹칩니다 -> " + col + " (컬럼을 하나씩 적으십시오)");
                    }
                    Object expr = Dyn.of("col", ref(src, col, scope.sources.size() > 1));
                    if (e.getValue() != 0) {
                        expr = Dyn.of("fn", "ADD_HOURS", "args", new ArrayList<>(List.of(expr, Dyn.of("num", (long) -e.getValue()))));
                    }
                    out.add(Dyn.of("expr", expr, "as", col));
                }
            }
            return out;
        }

        static String ref(Source src, String col, boolean qualify) {
            return qualify ? src.name + "." + col : col;
        }

        Object groupItem(Expression g, PlainSelect query, Scope scope, Map<String, Expression> aliases) {
            g = ident(g);
            if (g instanceof LongValue n) {
                return node(query.getSelectItems().get((int) n.getValue() - 1).getExpression(), scope, null);
            }
            if (g instanceof Column c && columnTable(c).isEmpty()) {
                // 입력 컬럼이 결과 별칭보다 먼저다 (PostgreSQL 규칙)
                String name = unquote(c.getColumnName());
                if (!scope.knows(name) && aliases.containsKey(name.toLowerCase())) {
                    return node(aliases.get(name.toLowerCase()), scope, null);
                }
            }
            return node(g, scope, null);
        }

        Map<String, Object> orderItem(OrderByElement o, Scope scope, List<String> names) {
            Expression target = ident(o.getExpression());
            String direction = o.isAsc() ? "asc" : "desc";
            if (target instanceof LongValue n) {
                return Dyn.of("expr", Dyn.of("col", names.get((int) n.getValue() - 1)), "dir", direction);
            }
            if (target instanceof Column c && columnTable(c).isEmpty()) {
                // 결과 별칭이 입력 컬럼보다 먼저다
                String name = unquote(c.getColumnName());
                for (String n : names) {
                    if (n.equalsIgnoreCase(name)) {
                        return Dyn.of("expr", Dyn.of("col", n), "dir", direction);
                    }
                }
            }
            return Dyn.of("expr", node(target, scope, null), "dir", direction);
        }

        // ── 조건 ───────────────────────────────────────────────────────

        List<Object> conditions(Expression node, Scope scope, Map<String, Expression> aliases) {
            List<Object> out = new ArrayList<>();
            for (Expression part : split(node, AndExpression.class)) {
                out.addAll(condition(part, scope, aliases));
            }
            return out;
        }

        /** 조건 하나 → 조건 목록 (BETWEEN 처럼 둘이 되기도 한다) */
        List<Object> condition(Expression node, Scope scope, Map<String, Expression> aliases) {
            Expression inside = parenInner(node);
            if (inside != null) {
                return conditions(inside, scope, aliases);
            }
            if (node instanceof OrExpression) {
                List<Object> group = new ArrayList<>();
                for (Expression part : split(node, OrExpression.class)) {
                    List<Object> inner = conditions(part, scope, aliases);
                    group.add(inner.size() == 1 ? inner.get(0) : Dyn.of("and", inner));
                }
                return new ArrayList<>(List.of(Dyn.of("or", group)));
            }
            boolean negate = false;
            if (node instanceof NotExpression not) {
                negate = true;
                node = not.getExpression();
                Expression unwrapped = parenInner(node);
                if (unwrapped != null) {
                    node = unwrapped;
                }
            }
            if (node instanceof IsBooleanExpression) {
                throw new IllegalArgumentException("IS 는 NULL 비교만 받습니다 -> " + sqlOf(node));
            }
            if (node instanceof IsNullExpression is) {
                boolean notNull = negate != is.isNot();
                return list(Dyn.of("expr", node(is.getLeftExpression(), scope, aliases), "op",
                        notNull ? "is_not_null" : "is_null"));
            }
            if (node instanceof InExpression in) {
                boolean not = negate != in.isNot();
                Expression right = in.getRightExpression();
                if (right instanceof Select query) {
                    Map<String, Object> inner = select(unwrapTextSelect(query));
                    if (Dyn.items(inner.get("select")).size() != 1) {
                        throw new IllegalArgumentException("IN 의 하위 쿼리는 SELECT 항목이 하나여야 합니다 -> " + sqlOf(node));
                    }
                    Object left = node(stripTextCast(in.getLeftExpression()), scope, aliases);
                    return list(Dyn.of("expr", left, "op", not ? "not_in" : "in", "value", Dyn.of("query", inner)));
                }
                List<Object> values = new ArrayList<>();
                for (Expression v : expressions(right)) {
                    Object literal = literal(v);
                    if (literal == NOT_LITERAL) {
                        throw new IllegalArgumentException("IN 목록은 상수만 받습니다 -> " + sqlOf(node));
                    }
                    values.add(literal);
                }
                return list(compare(in.getLeftExpression(), not ? "not_in" : "in", values, scope, aliases));
            }
            if (node instanceof LikeExpression like) {
                if (like.getLikeKeyWord() != null && like.getLikeKeyWord() != LikeExpression.KeyWord.LIKE) {
                    throw new IllegalArgumentException(like.getLikeKeyWord().name() + " 은(는) 지원하지 않습니다");
                }
                boolean not = negate != like.isNot();
                Object pattern = literal(like.getRightExpression());
                if (!(pattern instanceof String)) {
                    throw new IllegalArgumentException("LIKE 는 문자열 상수만 받습니다 -> " + sqlOf(node));
                }
                return list(Dyn.of("expr", node(like.getLeftExpression(), scope, aliases), "op", not ? "not_like" : "like",
                        "value", pattern));
            }
            if (node instanceof Between between && between.isNot()) {
                negate = !negate;
            }
            if (negate) {
                throw new IllegalArgumentException("NOT 은 IN · LIKE · IS NULL 에만 쓸 수 있습니다 -> NOT " + sqlOf(node));
            }
            if (node instanceof Between between) {
                return new ArrayList<>(List.of(
                        compare(between.getLeftExpression(), ">=", between.getBetweenExpressionStart(), scope, aliases),
                        compare(between.getLeftExpression(), "<=", between.getBetweenExpressionEnd(), scope, aliases)));
            }
            String op = null;
            Expression left = null;
            Expression right = null;
            if (node instanceof EqualsTo e) {
                op = "=";
                left = e.getLeftExpression();
                right = e.getRightExpression();
            } else if (node instanceof NotEqualsTo e) {
                op = "!=";
                left = e.getLeftExpression();
                right = e.getRightExpression();
            } else if (node instanceof GreaterThan e) {
                op = ">";
                left = e.getLeftExpression();
                right = e.getRightExpression();
            } else if (node instanceof GreaterThanEquals e) {
                op = ">=";
                left = e.getLeftExpression();
                right = e.getRightExpression();
            } else if (node instanceof MinorThan e) {
                op = "<";
                left = e.getLeftExpression();
                right = e.getRightExpression();
            } else if (node instanceof MinorThanEquals e) {
                op = "<=";
                left = e.getLeftExpression();
                right = e.getRightExpression();
            }
            if (op != null) {
                if (literal(left) != NOT_LITERAL && literal(right) == NOT_LITERAL) {
                    Expression swap = left;
                    left = right;
                    right = swap;
                    op = FLIP.get(op);
                }
                return list(compare(left, op, right, scope, aliases));
            }
            if (node instanceof ExistsExpression) {
                throw new IllegalArgumentException("EXISTS 은(는) 지원하지 않습니다");
            }
            throw new IllegalArgumentException("조건으로 쓸 수 없는 식입니다 -> " + sqlOf(node));
        }

        /** UTC 컬럼과 날짜 상수의 비교는 상수를 현지 시각으로 옮긴다 (컬럼은 그대로 둔다). right: 식, 또는 상수 목록 */
        Map<String, Object> compare(Expression left, String op, Object right, Scope scope, Map<String, Expression> aliases) {
            boolean many = right instanceof List && !(right instanceof Expression);
            List<Object> values = many ? new ArrayList<>((List<?>) right) : new ArrayList<>(Collections.singletonList(right));
            List<Object> literals = new ArrayList<>();
            List<LocalDateTime> moments = new ArrayList<>();
            for (Object v : values) {
                literals.add(v instanceof Expression e ? literal(e) : v);
                moments.add(moment(v));
            }
            Object[] utc = utcColumn(left, scope);
            if (utc != null && !moments.contains(null)) {
                int hours = (Integer) utc[1];
                List<Object> shifted = new ArrayList<>();
                for (LocalDateTime m : moments) {
                    shifted.add(m.plusHours(hours).format(STAMP));
                }
                return Dyn.of("expr", Dyn.of("col", utc[0]), "op", op, "value", many ? shifted : shifted.get(0));
            }
            Object expr = node(left, scope, aliases);
            if (many) {
                return Dyn.of("expr", expr, "op", op, "value", literals);
            }
            Object literal = literals.get(0);
            Object value = literal == NOT_LITERAL ? node((Expression) right, scope, aliases) : literal;
            return Dyn.of("expr", expr, "op", op, "value", value);
        }

        /** UTC 로 저장된 컬럼 하나면 [AST 참조, 시차] */
        Object[] utcColumn(Expression node, Scope scope) {
            Expression e = ident(unparen(node));
            if (!(e instanceof Column column)) {
                return null;
            }
            Source src = scope.owner(column).getValue();
            String name = find(src.columns, unquote(column.getColumnName()));
            int hours = name == null ? 0 : src.columns.getOrDefault(name, 0);
            if (hours == 0) {
                return null;
            }
            return new Object[] {ref(src, name, scope.sources.size() > 1), hours};
        }

        // ── 식 ─────────────────────────────────────────────────────────

        Object node(Expression node, Scope scope, Map<String, Expression> aliases) {
            Expression inside = parenInner(node);
            if (inside != null) {
                return node(inside, scope, aliases);
            }
            node = ident(node);
            if (node instanceof AllTableColumns || node instanceof AllColumns) {
                throw new IllegalArgumentException("t.* 는 SELECT 목록에만 쓸 수 있습니다");
            }
            if (node instanceof Column column) {
                String name = unquote(column.getColumnName());
                if (aliases != null && columnTable(column).isEmpty() && aliases.containsKey(name.toLowerCase())) {
                    if (!scope.knows(name)) {
                        return node(aliases.get(name.toLowerCase()), scope, aliases);
                    }
                }
                Source src = scope.owner(column).getValue();
                String real = find(src.columns, name);
                if (real == null) {
                    throw new IllegalArgumentException("없는 컬럼입니다 -> " + name);
                }
                Map<String, Object> ref = Dyn.of("col", ref(src, real, scope.sources.size() > 1));
                int hours = src.columns.getOrDefault(real, 0);
                if (hours != 0) {
                    warnings.add(real + " 은(는) UTC 로 저장된 컬럼인데 시차 보정 없이 썼습니다 (원본처럼 UTC 기준으로 옮겼습니다). "
                            + "현지 시각 기준이면 " + real + " + INTERVAL '" + hours + " hours' 로 적으십시오.");
                    return Dyn.of("fn", "ADD_HOURS", "args", new ArrayList<>(List.of(ref, Dyn.of("num", (long) -hours))));
                }
                return ref;
            }
            Object literal = literal(node);
            if (literal != NOT_LITERAL) {
                return literal instanceof String ? Dyn.of("str", literal) : Dyn.of("num", literal);
            }
            if (node instanceof SignedExpression signed) {
                if (signed.getSign() == '+') {
                    return node(signed.getExpression(), scope, aliases);
                }
                return Dyn.of("fn", "SUB", "args", new ArrayList<>(List.of(Dyn.of("num", 0L),
                        node(signed.getExpression(), scope, aliases))));
            }
            if (node instanceof Addition || node instanceof Subtraction) {
                Object shifted = intervalAdd((net.sf.jsqlparser.expression.BinaryExpression) node, scope, aliases);
                if (shifted != null) {
                    return shifted;
                }
            }
            String arithmetic = node instanceof Addition ? "ADD" : node instanceof Subtraction ? "SUB"
                    : node instanceof Multiplication ? "MUL" : node instanceof Division ? "DIV" : null;
            if (arithmetic != null) {
                var binary = (net.sf.jsqlparser.expression.BinaryExpression) node;
                Map<String, Object> left = Dyn.map(node(binary.getLeftExpression(), scope, aliases));
                Object right = node(binary.getRightExpression(), scope, aliases);
                // 같은 연산이 이어지면 하나로 (a - b - c → SUB(a, b, c)). DIV 는 두 값만.
                if (!arithmetic.equals("DIV") && arithmetic.equals(left.get("fn"))) {
                    List<Object> args = new ArrayList<>(Dyn.items(left.get("args")));
                    args.add(right);
                    return Dyn.of("fn", arithmetic, "args", args);
                }
                Object l = left;
                if (arithmetic.equals("DIV")) {
                    Object[] stripped = stripDivGuards(left, Dyn.map(right));
                    l = stripped[0];
                    right = stripped[1];
                }
                return Dyn.of("fn", arithmetic, "args", new ArrayList<>(List.of(l, right)));
            }
            if (node instanceof Function f) {
                return function(f, scope, aliases);
            }
            if (node instanceof TimeKeyExpression key) {
                String word = key.getStringValue().toUpperCase().replace("()", "");
                if (word.equals("CURRENT_DATE")) {
                    return Dyn.of("fn", "CURRENT_DATE", "args", new ArrayList<>());
                }
                if (word.equals("CURRENT_TIMESTAMP") || word.equals("NOW")) {
                    return Dyn.of("fn", "NOW", "args", new ArrayList<>());
                }
            }
            if (node instanceof AnalyticExpression window) {
                return window(window, scope, aliases);
            }
            if (node instanceof CaseExpression c) {
                return caseNode(c, scope, aliases);
            }
            if (node instanceof CastExpression cast) {
                if (isDateType(cast)) {
                    return Dyn.of("fn", "DATE", "args", new ArrayList<>(List.of(node(cast.getLeftExpression(), scope, aliases))));
                }
                throw new IllegalArgumentException("형 변환은 날짜(::date)만 받습니다 -> " + sqlOf(node));
            }
            if (node instanceof IntervalExpression) {
                throw new IllegalArgumentException("INTERVAL 은 날짜 ± INTERVAL 로만 씁니다 -> " + sqlOf(node));
            }
            throw new IllegalArgumentException("지원하지 않는 식입니다 -> " + sqlOf(node) + " (" + node.getClass().getSimpleName() + ")");
        }

        Object function(Function f, Scope scope, Map<String, Expression> aliases) {
            String name = unquote(f.getName()).toUpperCase();
            List<Expression> args = expressions(f.getParameters());
            if (AGGREGATES.containsKey(name) && args.size() == 1 && !f.isDistinct()) {
                return Dyn.of("fn", name, "args", new ArrayList<>(List.of(node(args.get(0), scope, aliases))));
            }
            if (name.equals("COUNT")) {
                if (f.isAllColumns() || args.isEmpty() || (args.size() == 1 && args.get(0) instanceof AllColumns)) {
                    return Dyn.of("fn", "COUNT", "args", new ArrayList<>());
                }
                if (f.isDistinct()) {
                    if (args.size() != 1) {
                        throw new IllegalArgumentException("COUNT(DISTINCT …) 는 컬럼 하나만 받습니다 -> " + sqlOf(f));
                    }
                    return Dyn.of("fn", "COUNT_DISTINCT", "args", new ArrayList<>(List.of(node(args.get(0), scope, aliases))));
                }
                if (args.size() == 1) {
                    return Dyn.of("fn", "COUNT", "args", new ArrayList<>(List.of(node(args.get(0), scope, aliases))));
                }
            }
            if (!f.isDistinct()) {
                if (name.equals("ABS") && args.size() == 1) {
                    return Dyn.of("fn", "ABS", "args", nodes(args, scope, aliases));
                }
                if (name.equals("ROUND") && (args.size() == 1 || args.size() == 2)) {
                    return Dyn.of("fn", "ROUND", "args", nodes(args, scope, aliases));
                }
                if (name.equals("NULLIF") && args.size() == 2) {
                    return Dyn.of("fn", "NULLIF", "args", nodes(args, scope, aliases));
                }
                if (SCALARS.contains(name) && !args.isEmpty()) {
                    return Dyn.of("fn", name, "args", nodes(args, scope, aliases));
                }
                if (name.equals("DATE") && args.size() == 1) {
                    return Dyn.of("fn", "DATE", "args", nodes(args, scope, aliases));
                }
                if (name.equals("DATE_TRUNC") && args.size() == 2) {
                    Object unit = literal(args.get(0));
                    String word = unit instanceof String s ? s.toLowerCase() : "";
                    if (!TRUNC.containsKey(word)) {
                        throw new IllegalArgumentException("DATE_TRUNC 단위는 hour · day · week · month · year 만 받습니다 -> " + sqlOf(f));
                    }
                    return Dyn.of("fn", "DATE_TRUNC", "unit", TRUNC.get(word), "args",
                            new ArrayList<>(List.of(node(args.get(1), scope, aliases))));
                }
                // Oracle 의 TRUNC(시각) · TRUNC(시각, '단위') — 컴파일러가 Oracle 에서 이렇게 만든다
                if (name.equals("TRUNC") && driver.equals("oracle") && args.size() == 1) {
                    return Dyn.of("fn", "DATE", "args", nodes(args, scope, aliases));
                }
                if (name.equals("TRUNC") && driver.equals("oracle") && args.size() == 2) {
                    Object unit = literal(args.get(1));
                    String word = unit instanceof String s ? s.toLowerCase() : "";
                    if (!TRUNC.containsKey(word)) {
                        throw new IllegalArgumentException("DATE_TRUNC 단위는 hour · day · week · month · year 만 받습니다 -> " + sqlOf(f));
                    }
                    return Dyn.of("fn", "DATE_TRUNC", "unit", TRUNC.get(word), "args",
                            new ArrayList<>(List.of(node(args.get(0), scope, aliases))));
                }
                if ((name.equals("NOW") || name.equals("CURRENT_TIMESTAMP")) && args.isEmpty()) {
                    return Dyn.of("fn", "NOW", "args", new ArrayList<>());
                }
                if (name.equals("CURRENT_DATE") && args.isEmpty()) {
                    return Dyn.of("fn", "CURRENT_DATE", "args", new ArrayList<>());
                }
            }
            throw new IllegalArgumentException("지원하지 않는 식입니다 -> " + sqlOf(f) + " (" + name + ")");
        }

        List<Object> nodes(List<Expression> args, Scope scope, Map<String, Expression> aliases) {
            List<Object> out = new ArrayList<>();
            for (Expression a : args) {
                out.add(node(a, scope, aliases));
            }
            return out;
        }

        /** 날짜 ± INTERVAL. UTC 컬럼 + 시차는 현지 시각 컬럼이 된다. */
        Object intervalAdd(net.sf.jsqlparser.expression.BinaryExpression node, Scope scope, Map<String, Expression> aliases) {
            if (!(node.getRightExpression() instanceof IntervalExpression interval)) {
                return null;
            }
            Object[] parsed = interval(interval);
            double n = (Double) parsed[0];
            String unit = (String) parsed[1];
            if (node instanceof Subtraction) {
                n = -n;
            }
            Object[] utc = utcColumn(node.getLeftExpression(), scope);
            if (utc != null && unit.equals("hours")) {
                int hours = (Integer) utc[1];
                double rest = n - hours;  // 원본: UTC 값 + n 시간 = 현지 값 + (n - 시차) 시간
                return rest == 0 ? Dyn.of("col", utc[0])
                        : Dyn.of("fn", "ADD_HOURS", "args", new ArrayList<>(List.of(Dyn.of("col", utc[0]), Dyn.of("num", whole(rest)))));
            }
            Object base = node(node.getLeftExpression(), scope, aliases);
            return Dyn.of("fn", INTERVAL_FN.get(unit), "args", new ArrayList<>(List.of(base, Dyn.of("num", whole(n)))));
        }

        /** CASE WHEN 조건 THEN 값 … ELSE 값 END. CASE x WHEN 1 THEN … 은 x = 1 조건으로 옮긴다. */
        Map<String, Object> caseNode(CaseExpression node, Scope scope, Map<String, Expression> aliases) {
            Expression subject = node.getSwitchExpression();
            List<Object> whens = new ArrayList<>();
            for (WhenClause branch : node.getWhenClauses()) {
                List<Object> cond;
                if (subject != null) {
                    cond = list(compare(subject, "=", branch.getWhenExpression(), scope, aliases));
                } else {
                    cond = conditions(branch.getWhenExpression(), scope, aliases);
                }
                whens.add(Dyn.of("if", cond, "then", node(branch.getThenExpression(), scope, aliases)));
            }
            Map<String, Object> out = Dyn.of("fn", "CASE", "when", whens);
            if (node.getElseExpression() != null) {
                out.put("else", node(node.getElseExpression(), scope, aliases));
            }
            return out;
        }

        Map<String, Object> window(AnalyticExpression node, Scope scope, Map<String, Expression> aliases) {
            if (!"ROW_NUMBER".equalsIgnoreCase(node.getName()) || node.getExpression() != null) {
                throw new IllegalArgumentException("윈도 함수는 ROW_NUMBER() 만 받습니다 -> " + node.getName() + "()");
            }
            if (node.getWindowElement() != null) {
                throw new IllegalArgumentException("윈도 범위(ROWS/RANGE)는 지원하지 않습니다");
            }
            Map<String, Object> out = Dyn.of("fn", "ROW_NUMBER", "args", new ArrayList<>());
            List<Expression> part = expressions(node.getPartitionExpressionList());
            if (!part.isEmpty()) {
                out.put("partition_by", nodes(part, scope, aliases));
            }
            if (node.getOrderByElements() == null || node.getOrderByElements().isEmpty()) {
                throw new IllegalArgumentException("ROW_NUMBER() 에는 ORDER BY 가 필요합니다");
            }
            List<Object> orders = new ArrayList<>();
            for (OrderByElement o : node.getOrderByElements()) {
                orders.add(Dyn.of("expr", node(o.getExpression(), scope, aliases), "dir", o.isAsc() ? "asc" : "desc"));
            }
            out.put("order_by", orders);
            return out;
        }

        /** 상수면 그 값 (문자열 · 숫자). 아니면 NOT_LITERAL */
        static Object literal(Expression node) {
            node = unparen(node);
            if (node instanceof CastExpression cast && cast.getLeftExpression() instanceof StringValue s) {
                return s.getValue();  // '2026-10-01'::timestamp · CAST('…' AS DATE)
            }
            if (node instanceof DateTimeLiteralExpression dt) {
                return stripQuotes(dt.getValue());  // TIMESTAMP '…'
            }
            if (node instanceof StringValue s) {
                return s.getValue();
            }
            if (node instanceof LongValue || node instanceof DoubleValue) {
                return number(node.toString());
            }
            if (node instanceof SignedExpression signed && signed.getSign() == '-'
                    && (signed.getExpression() instanceof LongValue || signed.getExpression() instanceof DoubleValue)) {
                Object n = number(signed.getExpression().toString());
                return n instanceof Long l ? (Object) (-l) : (Object) (-(Double) n);
            }
            return NOT_LITERAL;
        }
    }

    // ── 도구 ───────────────────────────────────────────────────────────────

    static List<Object> list(Object item) {
        return new ArrayList<>(List.of(item));
    }

    /** _number: 정수로 적혔으면 정수, 아니면 소수 */
    static Object number(String text) {
        double value = Double.parseDouble(text);
        if (value == Math.rint(value) && !text.contains(".") && Math.abs(value) < 9e18) {
            return (long) value;
        }
        return value;
    }

    /** 정수면 정수로 */
    static Object whole(double n) {
        return n == Math.rint(n) && Math.abs(n) < 9e18 ? (Object) (long) n : (Object) n;
    }

    /** INTERVAL '9 hours' · INTERVAL '9' HOUR · INTERVAL 9 HOUR → [9.0, "hours"] */
    static Object[] interval(IntervalExpression node) {
        String amount = node.getParameter() != null ? stripQuotes(node.getParameter())
                : node.getExpression() != null ? stripQuotes(node.getExpression().toString()) : "";
        amount = amount.strip();
        String word = node.getIntervalType() == null ? "" : node.getIntervalType().toLowerCase();
        if (word.isEmpty() && amount.contains(" ")) {
            String[] parts = amount.split("\\s+", 2);
            amount = parts[0];
            word = parts[1];
        }
        word = word.toLowerCase().strip();
        while (word.endsWith("s")) {
            word = word.substring(0, word.length() - 1);
        }
        double n;
        try {
            n = Double.parseDouble(amount);
        } catch (NumberFormatException error) {
            throw new IllegalArgumentException("INTERVAL 값을 읽을 수 없습니다 -> " + sqlOf(node));
        }
        return switch (word) {
            case "hour" -> new Object[] {n, "hours"};
            case "day" -> new Object[] {n, "days"};
            case "minute" -> new Object[] {n, "minutes"};
            case "second" -> new Object[] {n, "seconds"};
            default -> throw new IllegalArgumentException(
                    "INTERVAL 은 초(second)·분(minute)·시간(hour)·일(day) 단위만 받습니다 -> " + sqlOf(node));
        };
    }

    /** 날짜·시각 상수면 그 시각. '2026-10-01' · TIMESTAMP '…' · '…'::timestamp - INTERVAL '9 hours' 도 상수다. */
    static LocalDateTime moment(Object node) {
        if (node instanceof String s) {
            return Compiler.parseDatetime(s);
        }
        if (!(node instanceof Expression)) {
            return null;
        }
        Expression e = unparen((Expression) node);
        if ((e instanceof Addition || e instanceof Subtraction)
                && ((net.sf.jsqlparser.expression.BinaryExpression) e).getRightExpression() instanceof IntervalExpression iv) {
            LocalDateTime base = moment(((net.sf.jsqlparser.expression.BinaryExpression) e).getLeftExpression());
            if (base == null) {
                return null;
            }
            Object[] parsed = interval(iv);
            double n = (Double) parsed[0];
            long nanos = Math.round(n * switch ((String) parsed[1]) {
                case "seconds" -> 1e9;
                case "minutes" -> 60e9;
                case "hours" -> 3600e9;
                default -> 86400e9;
            });
            return e instanceof Addition ? base.plusNanos(nanos) : base.minusNanos(nanos);
        }
        if (e instanceof CastExpression cast) {
            e = cast.getLeftExpression();
        }
        if (e instanceof StringValue s) {
            return Compiler.parseDatetime(s.getValue());
        }
        if (e instanceof DateTimeLiteralExpression dt) {
            return Compiler.parseDatetime(stripQuotes(dt.getValue()));
        }
        return null;
    }

    /**
     * 문자로 바꾸는 형 변환(CAST … AS VARCHAR, ::text, TO_CHAR)을 벗긴다.
     * 타입이 다른 컬럼끼리의 비교에 붙는 것이라 AST 에는 두지 않는다 — 컴파일러가 다시 맞춘다.
     */
    static Expression stripTextCast(Expression node) {
        while (true) {
            Expression inside = parenInner(node);
            if (inside != null) {
                node = inside;
            } else if (node instanceof CastExpression cast && isTextType(cast)) {
                node = cast.getLeftExpression();
            } else if (node instanceof Function f && "TO_CHAR".equalsIgnoreCase(f.getName())
                    && expressions(f.getParameters()).size() == 1) {
                node = expressions(f.getParameters()).get(0);
            } else {
                return ident(node);
            }
        }
    }

    /** 컴파일러가 만든 SELECT CAST(x.c AS VARCHAR) FROM (안쪽 SELECT) x 를 안쪽 SELECT 로 되돌린다 */
    static Select unwrapTextSelect(Select query) {
        Select select = query instanceof ParenthesedSelect ps ? ps.getSelect() : query;
        if (!(select instanceof PlainSelect plain) || plain.getSelectItems().size() != 1) {
            return select;
        }
        Expression expr = plain.getSelectItems().get(0).getExpression();
        Expression col = stripTextCast(expr);
        if (col == expr || !(col instanceof Column column) || !(plain.getFromItem() instanceof ParenthesedSelect source)) {
            return select;
        }
        if ((plain.getJoins() != null && !plain.getJoins().isEmpty()) || plain.getWhere() != null || plain.getGroupBy() != null) {
            return select;
        }
        String alias = source.getAlias() == null ? "" : unquote(source.getAlias().getName());
        if (!alias.equals(columnTable(column))) {
            return select;
        }
        return source.getSelect();
    }

    /** WITH 의 결과 컬럼 이름 (컬럼 목록이 있으면 그것, 없으면 첫 SELECT 의 이름) */
    static List<String> cteNames(WithItem<?> cte) {
        List<String> names = new ArrayList<>();
        if (cte.getWithItemList() != null && !cte.getWithItemList().isEmpty()) {
            for (SelectItem<?> c : cte.getWithItemList()) {
                Expression e = c.getExpression();
                names.add(e instanceof Column col ? unquote(col.getColumnName()) : unquote(String.valueOf(e)));
            }
            return names;
        }
        Select body = cte.getSelect() == null ? null : cte.getSelect().getSelect();
        Select first = body instanceof SetOperationList union ? union.getSelects().get(0) : body;
        if (first instanceof PlainSelect plain) {
            for (SelectItem<?> e : plain.getSelectItems()) {
                if (e.getAlias() != null) {
                    names.add(unquote(e.getAlias().getName()));
                } else if (e.getExpression() instanceof Column col) {
                    names.add(unquote(col.getColumnName()));
                } else {
                    names.add(String.valueOf(e.getExpression()));
                }
            }
        }
        return names;
    }

    /** a AND b AND c → [a, b, c] */
    static List<Expression> split(Expression node, Class<? extends net.sf.jsqlparser.expression.BinaryExpression> cls) {
        List<Expression> out = new ArrayList<>();
        if (cls.isInstance(node)) {
            var binary = (net.sf.jsqlparser.expression.BinaryExpression) node;
            out.addAll(split(binary.getLeftExpression(), cls));
            out.addAll(split(binary.getRightExpression(), cls));
        } else {
            out.add(node);
        }
        return out;
    }

    /** DIV 는 스스로 `* 1.0` 과 `NULLIF(…, 0)` 를 붙이니, SQL 에 적힌 같은 장치는 뺀다 (뜻은 같다). */
    static Object[] stripDivGuards(Map<String, Object> left, Map<String, Object> right) {
        Object l = left;
        Object r = right;
        List<Object> leftArgs = Dyn.items(left.get("args"));
        if ("MUL".equals(left.get("fn")) && leftArgs.size() == 2 && Json.same(leftArgs.get(1), Dyn.of("num", 1L))) {
            l = leftArgs.get(0);
        }
        if (right != null && "NULLIF".equals(right.get("fn"))) {
            List<Object> rightArgs = Dyn.items(right.get("args"));
            if (rightArgs.size() == 2 && Json.same(rightArgs.get(1), Dyn.of("num", 0L))) {
                r = rightArgs.get(0);
            }
        }
        return new Object[] {l, r};
    }

    /** JOIN 의 ON (여럿이면 AND 로 잇는다) */
    static Expression onExpression(Join join) {
        Collection<Expression> on = join.getOnExpressions();
        if (on == null || on.isEmpty()) {
            return null;
        }
        Expression out = null;
        for (Expression e : on) {
            out = out == null ? e : new AndExpression(out, e);
        }
        return out;
    }

    static String joinLabel(Join join) {
        List<String> words = new ArrayList<>();
        if (join.isNatural()) {
            words.add("NATURAL");
        }
        if (join.isLeft()) {
            words.add("LEFT");
        }
        if (join.isRight()) {
            words.add("RIGHT");
        }
        if (join.isFull()) {
            words.add("FULL");
        }
        if (join.isOuter()) {
            words.add("OUTER");
        }
        if (join.isCross()) {
            words.add("CROSS");
        }
        if (join.isSemi()) {
            words.add("SEMI");
        }
        if (join.isApply()) {
            words.add("APPLY");
        }
        return String.join(" ", words);
    }

    /** 컬럼 자리의 키워드 이름 (day, month …) 은 컬럼으로 본다 — JSqlParser 는 날짜 단위로 읽는다 */
    static Expression ident(Expression node) {
        if (node instanceof DateUnitExpression unit) {
            // toString 은 대문자로 바꾸므로 적힌 글자를 쓴다
            String written = unit.getASTNode() != null && unit.getASTNode().jjtGetFirstToken() != null
                    ? unit.getASTNode().jjtGetFirstToken().image : unit.toString();
            return new Column(written);
        }
        return node;
    }

    /** 괄호 하나로 감싼 식이면 그 안의 식, 아니면 null */
    static Expression parenInner(Expression node) {
        if (node instanceof Parenthesis p) {
            return p.getExpression();
        }
        if (node instanceof ParenthesedExpressionList<?> list && list.size() == 1 && list.get(0) instanceof Expression e) {
            return e;
        }
        return null;
    }

    static Expression unparen(Expression node) {
        Expression inside = parenInner(node);
        while (inside != null) {
            node = inside;
            inside = parenInner(node);
        }
        return node;
    }

    /** 식 목록 (IN 목록, 함수 인자) */
    static List<Expression> expressions(Object list) {
        List<Expression> out = new ArrayList<>();
        if (list instanceof ExpressionList<?> items) {
            for (Object item : items) {
                out.add((Expression) item);
            }
        } else if (list instanceof Expression e) {
            out.add(e);
        }
        return out;
    }

    static boolean isNumberLiteral(Object e) {
        return e instanceof LongValue || e instanceof DoubleValue;
    }

    static boolean isDateType(CastExpression cast) {
        return cast.getColDataType() != null && baseType(cast.getColDataType().getDataType()).equals("date");
    }

    static boolean isTextType(CastExpression cast) {
        return cast.getColDataType() != null && TEXT_TYPES.contains(baseType(cast.getColDataType().getDataType()));
    }

    static String baseType(String dataType) {
        return dataType == null ? "" : dataType.split("\\(", 2)[0].strip().toLowerCase();
    }

    /** 컬럼의 테이블 한정자 ('t.c' 의 t). 없으면 빈 문자열 */
    static String columnTable(Column column) {
        Table table = column.getTable();
        return table == null || blank(table.getName()) ? "" : unquote(table.getName());
    }

    static boolean blank(String text) {
        return text == null || text.isBlank();
    }

    /** "이름" · `이름` · [이름] 의 따옴표를 뗀다 */
    static String unquote(String name) {
        if (name == null) {
            return "";
        }
        String text = name.strip();
        if (text.length() >= 2) {
            char a = text.charAt(0);
            char b = text.charAt(text.length() - 1);
            if ((a == '"' && b == '"') || (a == '`' && b == '`') || (a == '[' && b == ']')) {
                return text.substring(1, text.length() - 1);
            }
        }
        return text;
    }

    static String stripQuotes(String text) {
        String t = text == null ? "" : text.strip();
        if (t.length() >= 2 && t.charAt(0) == '\'' && t.charAt(t.length() - 1) == '\'') {
            return t.substring(1, t.length() - 1).replace("''", "'");
        }
        return t;
    }

    static String sqlOf(Object node) {
        return String.valueOf(node).strip();
    }

    static String firstLine(Exception error) {
        String text = error.getMessage();
        if (error.getCause() != null && error.getCause().getMessage() != null) {
            text = error.getCause().getMessage();
        }
        return text == null ? error.getClass().getSimpleName() : Py.firstLine(new RuntimeException(text));
    }

    /** 문장 종류 이름 (오류 메시지용): UNION, INSERT … */
    static String kindName(Object node) {
        if (node instanceof SetOperationList union && !union.getOperations().isEmpty()) {
            return union.getOperations().get(0).toString().split(" ")[0].toUpperCase();
        }
        if (node instanceof ParenthesedSelect ps) {
            return kindName(ps.getSelect());
        }
        return node == null ? "NONE" : node.getClass().getSimpleName().toUpperCase();
    }

    // ── 노드 찾기 (sqlglot 의 find_all) ───────────────────────────────────

    private static final Set<String> SKIP_FIELDS = Set.of("node", "parent", "children", "firstToken", "lastToken", "next");

    /** 문장 안의 테이블 (컬럼의 테이블 한정자는 빼고) */
    static List<Table> findTables(Object root) {
        List<Table> out = new ArrayList<>();
        for (Object node : findAll(root)) {
            if (node instanceof Table t) {
                out.add(t);
            }
        }
        return out;
    }

    /** JSqlParser 노드를 모두 (깊이 우선). 컬럼 · t.* 안의 테이블 한정자로는 내려가지 않는다. */
    static List<Object> findAll(Object root) {
        List<Object> out = new ArrayList<>();
        walk(root, out, Collections.newSetFromMap(new IdentityHashMap<>()));
        return out;
    }

    private static void walk(Object node, List<Object> out, Set<Object> seen) {
        if (node == null || !seen.add(node)) {
            return;
        }
        if (node instanceof Collection<?> items) {
            for (Object item : items) {
                walk(item, out, seen);
            }
            if (!node.getClass().getName().startsWith("net.sf.jsqlparser")) {
                return;
            }
        }
        if (!node.getClass().getName().startsWith("net.sf.jsqlparser")) {
            return;
        }
        out.add(node);
        if (node instanceof Column || node instanceof AllTableColumns || node instanceof Table) {
            return;
        }
        for (Class<?> c = node.getClass(); c != null && c != Object.class; c = c.getSuperclass()) {
            if (!c.getName().startsWith("net.sf.jsqlparser")) {
                continue;
            }
            for (Field f : c.getDeclaredFields()) {
                if (Modifier.isStatic(f.getModifiers()) || SKIP_FIELDS.contains(f.getName()) || f.getType().isPrimitive()) {
                    continue;
                }
                try {
                    f.setAccessible(true);
                    walk(f.get(node), out, seen);
                } catch (ReflectiveOperationException | RuntimeException ignored) {
                    // 읽지 못하는 필드는 건너뛴다
                }
            }
        }
    }
}
