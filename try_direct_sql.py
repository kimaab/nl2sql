"""지표 없이 질문 → SQL 임시 실험.

1) 테이블 고르기: 모든 테이블·컬럼 정보를 LLM 에 보내 쓸 테이블을 정한다.
2) AST 만들기: 고른 테이블의 컬럼 상세를 보내 LLM 이 AST(JSON) 를 만든다.
3) 컴파일: AST 를 SQL 로 조합하고 검증만 한다 (테이블·컬럼·함수·묶음 규칙). 업무 규칙은 넣지 않는다.

실행:
  python try_direct_sql.py "최근 5일 일별 무인 가동시간"
  python try_direct_sql.py "..." --repeat 5      같은 질문을 5번 돌려 SQL 이 같은지 본다
  python try_direct_sql.py "..." --explain       대상 DB 에서 EXPLAIN 으로 한 번 더 검증 (읽기 전용)
"""

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from contract import load_contract  # noqa: E402
from llm import make_model  # noqa: E402
from selector import parse_json, usage_of  # noqa: E402

DEFAULT_SYSTEM = "88e5f186-1f84-467a-8c47-8c0960aafe05"  # baroness개발서버
MAX_TRIES = 3


# ── 컴파일러: AST → SQL, 검증만 ─────────────────────────────────────────

AGGREGATES = {"SUM", "AVG", "MIN", "MAX", "COUNT", "COUNT_DISTINCT"}
ARITHMETIC = {"ADD": "+", "SUB": "-", "MUL": "*"}
SCALARS = {  # 이름: (최소, 최대 인자 수)
    "ABS": (1, 1),
    "ROUND": (1, 2),
    "COALESCE": (2, 8),
    "NULLIF": (2, 2),
    "GREATEST": (2, 8),
    "LEAST": (2, 8),
}
TRUNC_UNITS = {"hour", "day", "week", "month", "year"}
OPERATORS = {"=", "!=", ">", ">=", "<", "<="}


class AstError(ValueError):
    pass


def q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


class Scope:
    """FROM 절이 보이는 이름들: {별칭: {컬럼}}"""

    def __init__(self):
        self.sources: dict[str, set] = {}

    def add(self, alias: str, columns):
        if alias in self.sources:
            raise AstError(f"FROM 별칭이 겹칩니다 -> {alias}")
        self.sources[alias] = set(columns)

    def column(self, ref: str) -> str:
        if "." in ref:
            alias, col = ref.split(".", 1)
            if alias not in self.sources:
                raise AstError(f"FROM 에 없는 테이블입니다 -> {alias} (있는 것: {', '.join(self.sources)})")
            if col not in self.sources[alias]:
                raise AstError(f"{alias} 에 없는 컬럼입니다 -> {col}")
            return f"{q(alias)}.{q(col)}"
        owners = [a for a, cols in self.sources.items() if ref in cols]
        if not owners:
            known = sorted(set().union(*self.sources.values()))
            raise AstError(f"없는 컬럼입니다 -> {ref} (쓸 수 있는 것: {', '.join(known)})")
        if len(owners) > 1:
            raise AstError(f"여러 테이블에 있는 컬럼입니다 -> {ref} ('테이블.컬럼' 으로 적으십시오)")
        return q(ref) if len(self.sources) == 1 else f"{q(owners[0])}.{q(ref)}"


class MiniCompiler:
    def __init__(self, contract: dict):
        self.tables = {t["name"]: [c["name"] for c in t["columns"]] for t in contract["tables"]}

    def compile(self, ast: dict) -> str:
        return self._query(ast, depth=0) + ";"

    # 쿼리 한 단계. from 에 AST 를 넣으면 하위 쿼리가 된다.
    def _query(self, ast: dict, depth: int) -> str:
        if not isinstance(ast, dict):
            raise AstError("AST 는 객체입니다")
        if depth > 3:
            raise AstError("하위 쿼리는 3단계까지입니다")
        known = {"from", "joins", "select", "where", "group_by", "having", "order_by", "limit"}
        unknown = set(ast) - known
        if unknown:
            raise AstError(f"모르는 키입니다 -> {', '.join(sorted(unknown))} (쓸 수 있는 키: {', '.join(sorted(known))})")

        scope = Scope()
        source = ast.get("from")
        if isinstance(source, str):
            if source not in self.tables:
                raise AstError(f"없는 테이블입니다 -> {source}")
            scope.add(source, self.tables[source])
            from_sql = q(source)
        elif isinstance(source, dict) and "query" in source:
            alias = str(source.get("as") or "sub")
            inner = source["query"]
            from_sql = f"({self._query(inner, depth + 1)}) {q(alias)}"
            scope.add(alias, [s["as"] for s in inner["select"]])
        else:
            raise AstError('from 은 테이블 이름이나 {"query": AST, "as": "별칭"} 입니다')

        for join in ast.get("joins") or []:
            table = join.get("table")
            if table not in self.tables:
                raise AstError(f"없는 테이블입니다 -> {table}")
            scope.add(table, self.tables[table])
            kind = {"inner": "JOIN", "left": "LEFT JOIN"}.get(str(join.get("type", "inner")).lower())
            if kind is None:
                raise AstError("join type 은 inner 나 left 입니다")
            pairs = join.get("on") or []
            if not pairs:
                raise AstError(f"{table} 조인에 on 이 없습니다")
            on = " AND ".join(f"{scope.column(a)} = {scope.column(b)}" for a, b in pairs)
            from_sql += f" {kind} {q(table)} ON {on}"

        select = ast.get("select") or []
        if not select:
            raise AstError("select 가 비었습니다")
        names, select_sql, has_agg = set(), [], False
        for item in select:
            if not isinstance(item, dict) or "expr" not in item or not item.get("as"):
                raise AstError('select 항목은 {"expr": 식, "as": "이름"} 입니다')
            if item["as"] in names:
                raise AstError(f"select 이름이 겹칩니다 -> {item['as']}")
            names.add(item["as"])
            sql, info = self._expr(item["expr"], scope)
            has_agg |= info["agg"]
            select_sql.append((sql, info, item["as"]))

        group_sql = []
        for e in ast.get("group_by") or []:
            sql, info = self._expr(e, scope)
            if info["agg"] or info["window"]:
                raise AstError("group_by 에 집계·윈도 함수를 쓸 수 없습니다")
            group_sql.append(sql)
        if has_agg or group_sql:
            for sql, info, name in select_sql:
                if not info["agg"] and not info["window"] and info["cols"] and sql not in group_sql:
                    raise AstError(f"집계하지 않은 select 항목은 group_by 에 같은 식으로 있어야 합니다 -> {name}")

        parts = ["SELECT " + ", ".join(f"{s} AS {q(n)}" for s, _, n in select_sql), "FROM " + from_sql]
        where = self._conditions(ast.get("where"), scope, allow_agg=False)
        if where:
            parts.append("WHERE " + where)
        if group_sql:
            parts.append("GROUP BY " + ", ".join(group_sql))
        having = self._conditions(ast.get("having"), scope, allow_agg=True)
        if having:
            if not (has_agg or group_sql):
                raise AstError("having 은 집계할 때만 씁니다")
            parts.append("HAVING " + having)
        order = []
        for item in ast.get("order_by") or []:
            target = item.get("expr")
            if isinstance(target, dict) and target.get("col") in names:
                sql = q(target["col"])  # select 이름으로 정렬
            else:
                sql = self._expr(target, scope)[0]
            way = str(item.get("dir", "asc")).lower()
            if way not in ("asc", "desc"):
                raise AstError("order_by dir 은 asc 나 desc 입니다")
            order.append(f"{sql} {way.upper()}")
        if order:
            parts.append("ORDER BY " + ", ".join(order))
        if ast.get("limit") is not None:
            limit = ast["limit"]
            if not isinstance(limit, int) or not 1 <= limit <= 10000:
                raise AstError("limit 은 1~10000 정수입니다")
            parts.append(f"LIMIT {limit}")
        return " ".join(parts)

    def _conditions(self, items, scope: Scope, allow_agg: bool) -> str:
        out = []
        for c in items or []:
            if isinstance(c, dict) and "or" in c:
                inner = self._conditions(c["or"], scope, allow_agg).split(" AND ")
                out.append("(" + " OR ".join(inner) + ")")
                continue
            if not isinstance(c, dict) or "expr" not in c or "op" not in c:
                raise AstError('조건은 {"expr": 식, "op": "=", "value": 값} 입니다')
            left, info = self._expr(c["expr"], scope)
            if info["window"] or (info["agg"] and not allow_agg):
                raise AstError("where 에는 집계·윈도 함수를 쓸 수 없습니다 (집계 조건은 having, 순위 조건은 하위 쿼리 바깥에서)")
            op = str(c["op"]).lower()
            if op in ("is_null", "is_not_null"):
                out.append(f"{left} {'IS NULL' if op == 'is_null' else 'IS NOT NULL'}")
                continue
            if op in ("in", "not_in"):
                values = c.get("value")
                if not isinstance(values, list) or not values:
                    raise AstError("in 의 value 는 값 목록입니다")
                out.append(f"{left} {'IN' if op == 'in' else 'NOT IN'} ({', '.join(literal(v) for v in values)})")
                continue
            if op == "like":
                out.append(f"{left} LIKE {literal(c.get('value'))}")
                continue
            if op not in OPERATORS:
                raise AstError(f"모르는 연산자입니다 -> {op}")
            value = c.get("value")
            right = self._expr(value, scope)[0] if isinstance(value, dict) else literal(value)
            out.append(f"{left} {op} {right}")
        return " AND ".join(out)

    # 식 → (SQL, {agg, window, cols})
    def _expr(self, e, scope: Scope, in_agg: bool = False):
        none = {"agg": False, "window": False, "cols": False}
        if not isinstance(e, dict):
            raise AstError(f"식은 객체입니다 -> {e!r}")
        if "col" in e:
            return scope.column(str(e["col"])), {**none, "cols": True}
        if "num" in e:
            if not isinstance(e["num"], (int, float)) or isinstance(e["num"], bool):
                raise AstError(f"num 은 숫자입니다 -> {e['num']!r}")
            return repr(e["num"]), none
        if "str" in e:
            return literal(str(e["str"])), none
        fn = str(e.get("fn", "")).upper()
        args = e.get("args") or []
        if not fn:
            raise AstError(f'식은 {{"col"}}, {{"num"}}, {{"str"}}, {{"fn", "args"}} 중 하나입니다 -> {e!r}')

        def subs(items, inside=in_agg):
            return [self._expr(a, scope, inside) for a in items]

        def merge(parts, **extra):
            return {
                "agg": any(p[1]["agg"] for p in parts),
                "window": any(p[1]["window"] for p in parts),
                "cols": any(p[1]["cols"] for p in parts),
                **extra,
            }

        if fn in AGGREGATES:
            if in_agg:
                raise AstError(f"집계 안에 집계를 쓸 수 없습니다 -> {fn} (두 단계 집계는 from 에 하위 쿼리를 쓰십시오)")
            if fn == "COUNT" and not args:
                return "COUNT(*)", {**none, "agg": True}
            if len(args) != 1:
                raise AstError(f"{fn} 은 인자 하나입니다")
            (sql, info), = subs(args, inside=True)
            if info["window"]:
                raise AstError("집계 안에 윈도 함수를 쓸 수 없습니다")
            body = f"COUNT(DISTINCT {sql})" if fn == "COUNT_DISTINCT" else f"{fn}({sql})"
            return body, {**info, "agg": True}
        if fn in ARITHMETIC:
            if len(args) < 2:
                raise AstError(f"{fn} 은 인자 둘 이상입니다")
            parts = subs(args)
            return "(" + f" {ARITHMETIC[fn]} ".join(p[0] for p in parts) + ")", merge(parts)
        if fn == "DIV":
            if len(args) != 2:
                raise AstError("DIV 는 인자 둘입니다")
            (a, ia), (b, ib) = subs(args)
            return f"({a} * 1.0 / NULLIF({b}, 0))", merge([(a, ia), (b, ib)])
        if fn in SCALARS:
            lo, hi = SCALARS[fn]
            if not lo <= len(args) <= hi:
                raise AstError(f"{fn} 은 인자 {lo}~{hi}개입니다")
            parts = subs(args)
            return f"{fn}({', '.join(p[0] for p in parts)})", merge(parts)
        if fn == "DATE":
            if len(args) != 1:
                raise AstError("DATE 는 인자 하나입니다")
            (sql, info), = subs(args)
            return f"CAST({sql} AS DATE)", info
        if fn == "DATE_TRUNC":
            unit = str(e.get("unit", "")).lower()
            if unit not in TRUNC_UNITS or len(args) != 1:
                raise AstError(f'DATE_TRUNC 는 {{"fn": "DATE_TRUNC", "unit": "{"|".join(sorted(TRUNC_UNITS))}", "args": [식]}} 입니다')
            (sql, info), = subs(args)
            return f"DATE_TRUNC('{unit}', {sql})", info
        if fn in ("ADD_HOURS", "ADD_DAYS"):
            if len(args) != 2 or "num" not in (args[1] or {}):
                raise AstError(f"{fn} 은 [식, {{\"num\": 숫자}}] 입니다")
            (sql, info), = subs(args[:1])
            unit = "hours" if fn == "ADD_HOURS" else "days"
            return f"({sql} + INTERVAL '{float(args[1]['num']):g} {unit}')", info
        if fn in ("NOW", "CURRENT_DATE"):
            return ("CURRENT_TIMESTAMP" if fn == "NOW" else "CURRENT_DATE"), none
        if fn == "ROW_NUMBER":
            if in_agg:
                raise AstError("집계 안에 윈도 함수를 쓸 수 없습니다")
            part = subs(e.get("partition_by") or [])
            order = []
            for item in e.get("order_by") or []:
                way = str(item.get("dir", "asc")).lower()
                if way not in ("asc", "desc"):
                    raise AstError("order_by dir 은 asc 나 desc 입니다")
                order.append(f"{self._expr(item.get('expr'), scope)[0]} {way.upper()}")
            if not order:
                raise AstError("ROW_NUMBER 에는 order_by 가 필요합니다")
            over = (f"PARTITION BY {', '.join(p[0] for p in part)} " if part else "") + "ORDER BY " + ", ".join(order)
            return f"ROW_NUMBER() OVER ({over})", {**merge(part), "window": True}
        raise AstError(
            f"모르는 함수입니다 -> {fn} (쓸 수 있는 것: "
            + ", ".join(sorted(AGGREGATES | set(ARITHMETIC) | set(SCALARS)
                               | {"DIV", "DATE", "DATE_TRUNC", "ADD_HOURS", "ADD_DAYS", "NOW", "CURRENT_DATE", "ROW_NUMBER"}))
            + ")"
        )


# ── LLM 단계 ─────────────────────────────────────────────────────────────

TABLE_SYSTEM = """당신은 업무 시스템 '{name}' 의 테이블 목록에서 질문에 답할 테이블을 고르는 역할입니다.
[업무 영역]
{domain}
[규칙]
- 질문에 답하는 데 꼭 필요한 테이블만 고릅니다 (보통 하나, 조인이 필요할 때만 여럿).
- 아래 목록의 테이블 이름을 그대로 적습니다.
- 설명 없이 JSON 객체 하나로만 답합니다: {{"tables": ["테이블"], "reason": "고른 이유"}}"""

AST_SYSTEM = """당신은 질문을 SQL 조회 명세(AST, JSON)로 바꾸는 역할입니다. DB 는 PostgreSQL 입니다.
[업무 영역]
{domain}
[오늘]
{today}

[AST 모양]
{{
  "from": "테이블" 또는 {{"query": 하위 AST, "as": "별칭"}},
  "joins": [{{"table": "테이블", "type": "inner|left", "on": [["a.컬럼", "b.컬럼"]]}}],   (필요할 때만)
  "select": [{{"expr": 식, "as": "결과 이름"}}],
  "where": [조건], "group_by": [식], "having": [조건],
  "order_by": [{{"expr": 식, "dir": "asc|desc"}}], "limit": 숫자
}}
식: {{"col": "컬럼"}} | {{"num": 1}} | {{"str": "문자"}} | {{"fn": "함수", "args": [식, ...]}}
  집계: SUM AVG MIN MAX COUNT(인자 없으면 *) COUNT_DISTINCT
  계산: ADD SUB MUL DIV(0 으로 나누면 NULL)
  함수: ABS ROUND COALESCE NULLIF GREATEST LEAST
  날짜: DATE(날짜만), {{"fn": "DATE_TRUNC", "unit": "hour|day|week|month|year", "args": [식]}},
        ADD_HOURS [식, {{"num": n}}], ADD_DAYS [식, {{"num": n}}], NOW, CURRENT_DATE
  순위: {{"fn": "ROW_NUMBER", "partition_by": [식], "order_by": [{{"expr": 식, "dir": "desc"}}]}}
조건: {{"expr": 식, "op": "= != > >= < <= in not_in like is_null is_not_null", "value": 값 또는 식}}
      여럿은 AND. OR 은 {{"or": [조건, ...]}}

[규칙]
- 집계 안에 집계는 쓸 수 없습니다. 두 단계 집계(예: 장비·일별로 먼저 구한 뒤 일별 합계)는 from 에 하위 쿼리를 둡니다.
- 하위 쿼리 바깥에서는 하위 쿼리 select 의 "as" 이름을 컬럼으로 씁니다.
- 순위로 거르려면(예: 단위별 마지막 건) 하위 쿼리에서 ROW_NUMBER 를 구하고 바깥 where 에서 거릅니다.
- 집계하지 않은 select 항목은 group_by 에 같은 식으로 넣습니다.
- 컬럼 설명의 [UTC+n] 은 그 시각이 UTC 로 저장되어 있다는 뜻입니다. 날짜로 묶거나 표시할 때는 ADD_HOURS 로 n 시간을 더하십시오.
- 설명 없이 JSON 객체 하나로만 답합니다: {{"ast": AST, "reason": "설명"}}"""


def column_line(c: dict) -> str:
    note = c.get("description") or ""
    if c.get("utc_offset"):
        note = f"{note} [UTC+{c['utc_offset']}]".strip()
    return f"  - {c['name']} ({c.get('type', '')}){': ' + note if note else ''}"


def ask(model, system: str, messages: list, stats: dict) -> str:
    started = time.monotonic()
    reply = model.invoke([SystemMessage(system), *messages])
    p, c = usage_of(reply)
    stats["tokens"] += p + c
    stats["ms"] += int((time.monotonic() - started) * 1000)
    return reply.content


def pick_tables(model, contract: dict, question: str, stats: dict) -> list:
    lines = []
    for t in contract["tables"]:
        lines.append(f"- {t['name']}: {t.get('description') or '(설명 없음)'}")
        lines.append("  컬럼: " + ", ".join(
            f"{c['name']}({c['description']})" if c.get("description") else c["name"] for c in t["columns"]
        ))
    system = TABLE_SYSTEM.format(name=contract["name"], domain=contract.get("domain") or "")
    messages = [HumanMessage("[테이블 목록]\n" + "\n".join(lines) + f"\n\n[질문]\n{question}")]
    names = {t["name"] for t in contract["tables"]}
    for _ in range(MAX_TRIES):
        content = ask(model, system, messages, stats)
        try:
            tables = (parse_json(content) or {}).get("tables") or []
            bad = [t for t in tables if t not in names]
            if not tables or bad:
                raise ValueError(f"목록에 없는 테이블입니다 -> {bad or '(비었음)'}")
            return tables
        except (ValueError, json.JSONDecodeError) as error:
            messages += [SystemMessage(content), HumanMessage(f"형식 오류: {error}. 다시 답하십시오.")]
    raise RuntimeError("테이블을 고르지 못했습니다")


def build_ast(model, contract: dict, tables: list, question: str, compiler: MiniCompiler, stats: dict):
    blocks = []
    for t in contract["tables"]:
        if t["name"] in tables:
            blocks.append(f"[{t['name']}] {t.get('description') or ''}\n" + "\n".join(map(column_line, t["columns"])))
    for r in contract.get("relations") or []:
        if r["left_table"] in tables and r["right_table"] in tables:
            blocks.append(f"[관계] {r['left_table']}.{r['left_column']} = {r['right_table']}.{r['right_column']}")
    system = AST_SYSTEM.format(domain=contract.get("domain") or "", today=time.strftime("%Y-%m-%d"))
    messages = [HumanMessage("[테이블]\n" + "\n\n".join(blocks) + f"\n\n[질문]\n{question}")]
    last = None
    for attempt in range(1, MAX_TRIES + 1):
        content = ask(model, system, messages, stats)
        try:
            ast = (parse_json(content) or {}).get("ast")
            sql = compiler.compile(ast)
            return ast, sql, attempt
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            last = error
            print(f"  · 시도 {attempt} 실패: {error}")
            messages += [SystemMessage(content), HumanMessage(f"컴파일 오류: {error}\n고쳐서 JSON 으로 다시 답하십시오.")]
    raise RuntimeError(f"AST 를 만들지 못했습니다: {last}")


def explain(system_id: UUID, sql: str) -> str:
    import psycopg
    from psycopg.conninfo import make_conninfo

    from systems import connection_row

    r = connection_row(system_id)
    conninfo = make_conninfo(host=r["host"], port=r["port"], user=r["username"], password=r["password"],
                             dbname=r["db_name"], connect_timeout=5)
    with psycopg.connect(conninfo) as conn:
        conn.read_only = True
        conn.execute("SET statement_timeout = '30s'")
        conn.execute("EXPLAIN " + sql.rstrip(";"))
    return "EXPLAIN 통과"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--system", default=DEFAULT_SYSTEM)
    args = ap.parse_args()

    system_id = UUID(args.system)
    contract = load_contract(system_id)
    compiler = MiniCompiler(contract)
    model = make_model()
    print(f"질문: {args.question}\n모델: {getattr(model, 'model_name', '')}\n")

    results = []
    for run in range(1, args.repeat + 1):
        stats = {"tokens": 0, "ms": 0}
        print(f"── 실행 {run}/{args.repeat}")
        try:
            tables = pick_tables(model, contract, args.question, stats)
            print(f"  테이블: {tables}")
            ast, sql, attempt = build_ast(model, contract, tables, args.question, compiler, stats)
            print(f"  AST: {json.dumps(ast, ensure_ascii=False)}")
            print(f"  SQL ({attempt}번째 시도): {sql}")
            if args.explain:
                try:
                    print(f"  {explain(system_id, sql)}")
                except Exception as error:  # 대상 DB 오류를 그대로 보여 준다
                    print(f"  EXPLAIN 실패: {str(error).strip()}")
            results.append(sql)
        except RuntimeError as error:
            print(f"  실패: {error}")
            results.append(None)
        print(f"  ({stats['ms'] / 1000:.1f}초, 토큰 {stats['tokens']})\n")

    if args.repeat > 1:
        distinct = {r for r in results if r}
        print(f"== {args.repeat}번 중 성공 {sum(1 for r in results if r)}번, 서로 다른 SQL {len(distinct)}개")
        for i, sql in enumerate(sorted(distinct), 1):
            print(f"  [{i}] x{results.count(sql)}  {sql}")


if __name__ == "__main__":
    main()
