#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PostgreSQL 쿼리 포매터 (표준 라이브러리만 사용)

사용 예:
    python pg_format.py query.sql               # 파일을 포맷해서 stdout 출력
    python pg_format.py query.sql -o out.sql    # 결과를 파일로 저장
    cat query.sql | python pg_format.py         # stdin 입력
    python pg_format.py query.sql --check       # CI용: 포맷이 필요하면 종료코드 1

특징
  - 문자열('...'), 달러 인용($$...$$), 큰따옴표 식별자, 주석을 그대로 보존
  - 주요 절(SELECT, FROM, WHERE, GROUP BY, ORDER BY, LIMIT 등)마다 줄바꿈
  - SELECT 목록, GROUP BY, ORDER BY, SET 항목은 한 줄에 하나씩 정렬
  - JOIN / ON / AND / OR 정렬
  - 서브쿼리, CTE(WITH), CASE WHEN 들여쓰기
  - 키워드와 주요 함수명 대문자화 (--keep-case 로 끌 수 있음)
  - 포맷 전후 토큰 시퀀스를 비교해 쿼리 의미가 바뀌지 않았는지 검증
"""

import argparse
import re
import sys

# ---------------------------------------------------------------------------
# 키워드 / 함수 / 절 정의
# ---------------------------------------------------------------------------

KEYWORDS = set("""
ADD ALL ALTER AND ANY ARRAY AS ASC BETWEEN BY CASE CAST CHECK COLUMN CONFLICT CONSTRAINT
CREATE CROSS CURRENT_DATE CURRENT_TIMESTAMP DEFAULT DELETE DESC DISTINCT DO DROP ELSE END
EXCEPT EXISTS EXTRACT FALSE FETCH FILTER FOR FOREIGN FROM FULL GROUP HAVING ILIKE IN
INNER INSERT INTERSECT INTO IS JOIN LATERAL LEFT LIKE LIMIT NATURAL NOT NULL NULLS
OFFSET ON OR ORDER OUTER OVER PARTITION PRIMARY REFERENCES RETURNING RIGHT
SELECT SET SOME TABLE THEN TRUE UNION UNIQUE UPDATE USING VALUES WHEN WHERE WINDOW WITH
RECURSIVE ONLY MATERIALIZED NOTHING EXCLUDED
""".split())

FUNCTIONS = set("""
COUNT SUM AVG MIN MAX COALESCE NULLIF GREATEST LEAST ROUND TO_CHAR TO_DATE TO_TIMESTAMP
ROW_NUMBER RANK DENSE_RANK LAG LEAD FIRST_VALUE LAST_VALUE NTILE ARRAY_AGG STRING_AGG
JSON_AGG JSONB_AGG JSON_BUILD_OBJECT JSONB_BUILD_OBJECT NOW LOWER UPPER TRIM SUBSTRING
LENGTH CONCAT DATE_TRUNC DATE_PART ARRAY_LENGTH UNNEST GENERATE_SERIES
""".split())

# 이 절 키워드는 줄의 시작에 놓인다 (괄호 안에서는 무시)
BLOCK = {
    "WITH", "SELECT", "FROM", "WHERE", "GROUP BY", "HAVING", "ORDER BY", "LIMIT",
    "OFFSET", "UNION", "INTERSECT", "EXCEPT", "RETURNING", "SET", "VALUES",
    "INSERT INTO", "UPDATE", "DELETE FROM", "ON CONFLICT", "WINDOW",
}

# 이 키워드 뒤의 첫 항목은 다음 줄로 내린다
BREAK_AFTER = {"SELECT", "GROUP BY", "ORDER BY", "SET", "RETURNING", "VALUES"}

# 이 절에서 쉼표가 나오면 항목마다 줄을 바꾼다
COMMA_CLAUSES = {"SELECT", "FROM", "WITH", "GROUP BY", "ORDER BY", "SET", "RETURNING", "VALUES"}

# 두 단어가 붙어서 하나의 키워드가 되는 경우
MERGE_PAIRS = {
    ("GROUP", "BY"), ("ORDER", "BY"), ("PARTITION", "BY"),
    ("INSERT", "INTO"), ("DELETE", "FROM"), ("ON", "CONFLICT"),
}

JOIN_WORDS = {"LEFT", "RIGHT", "FULL", "INNER", "CROSS", "NATURAL", "OUTER", "LATERAL"}

# 뒤에 여는 괄호가 오면 띄어쓰기를 하는 키워드 (그 외 단어 + "("는 함수 호출로 보고 붙여 씀)
SPACE_BEFORE_PAREN = {
    "IN", "EXISTS", "AS", "ON", "AND", "OR", "NOT", "FROM", "JOIN", "WHERE", "SELECT",
    "WITH", "VALUES", "USING", "WHEN", "THEN", "ELSE", "BETWEEN", "LIKE", "ILIKE", "ANY",
    "ALL", "SOME", "HAVING", "BY", "OVER", "FILTER", "SET", "RETURNING", "IS", "DISTINCT",
    "UNION", "EXCEPT", "INTERSECT", "TABLE", "INTO", "LATERAL", "CASE", "END", "INSERT",
    "UPDATE", "RETURNS",
}

NO_SPACE_BEFORE = {")", ",", ";", ".", "::", "]", "["}
NO_SPACE_AFTER = {"(", ".", "::", "["}

OPERATORS = [
    "!~~*", "!~~", "!~*", "!~", "~~*", "~~", "~*",
    "->>", "#>>", "<=>", "<=", ">=", "<>", "!=", "::", "||", "->", "#>", "=>", ":=",
    "@>", "<@", "&&",
]

WORD_RE = re.compile(r"[A-Za-z_\u0080-\uffff][A-Za-z0-9_$\u0080-\uffff]*|\$\d+")
NUM_RE = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
DOLLAR_RE = re.compile(r"\$([A-Za-z_]\w*)?\$")


# ---------------------------------------------------------------------------
# 토큰화
# ---------------------------------------------------------------------------

def tokenize(sql):
    """SQL 문자열을 (종류, 텍스트) 토큰 리스트로 나눈다. 공백은 버린다."""
    toks = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            toks.append(("comment", sql[i:j].rstrip()))
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            toks.append(("comment", sql[i:j]))
            i = j
        elif c == "'":
            # 문자열 리터럴: '' 는 이스케이프된 따옴표
            j = i + 1
            while True:
                k = sql.find("'", j)
                if k == -1:
                    j = n
                    break
                if sql.startswith("''", k):
                    j = k + 2
                    continue
                j = k + 1
                break
            toks.append(("string", sql[i:j]))
            i = j
        elif c == '"':
            j = sql.find('"', i + 1)
            j = n if j == -1 else j + 1
            toks.append(("ident", sql[i:j]))
            i = j
        elif c == "$" and DOLLAR_RE.match(sql, i):
            tag = DOLLAR_RE.match(sql, i).group(0)
            j = sql.find(tag, i + len(tag))
            j = n if j == -1 else j + len(tag)
            toks.append(("string", sql[i:j]))
            i = j
        else:
            m = WORD_RE.match(sql, i)
            kind = "word"
            if not m and c.isdigit():
                m = NUM_RE.match(sql, i)
                kind = "number"
            if m:
                toks.append((kind, m.group(0)))
                i = m.end()
            else:
                op = next((o for o in OPERATORS if sql.startswith(o, i)), c)
                toks.append(("op", op))
                i += len(op)
    return toks


def _merge(tokens):
    """여러 단어로 된 키워드(GROUP BY, LEFT OUTER JOIN 등)를 하나의 토큰으로 합친다."""
    merged = []
    i, n = 0, len(tokens)
    while i < n:
        kind, text = tokens[i]
        if kind == "word":
            u = text.upper()
            if u in JOIN_WORDS or u == "JOIN":
                j, parts = i, []
                while j < n and tokens[j][0] == "word" and (
                    tokens[j][1].upper() in JOIN_WORDS or tokens[j][1].upper() == "JOIN"
                ):
                    parts.append(tokens[j][1])
                    j += 1
                    if parts[-1].upper() == "JOIN":
                        break
                if parts[-1].upper() == "JOIN":
                    merged.append(("kw", " ".join(parts)))
                    i = j
                    continue
            if i + 1 < n and tokens[i + 1][0] == "word" and (
                u, tokens[i + 1][1].upper()
            ) in MERGE_PAIRS:
                merged.append(("kw", f"{text} {tokens[i + 1][1]}"))
                i += 2
                continue
        merged.append(tokens[i])
        i += 1
    return merged


# ---------------------------------------------------------------------------
# 출력 버퍼
# ---------------------------------------------------------------------------

class _Writer:
    def __init__(self, pad):
        self.pad = pad
        self.lines = []
        self.cur = ""
        self.level = 0  # 현재 줄의 들여쓰기 단계

    def newline(self, level):
        if self.cur.strip():
            self.lines.append(self.cur.rstrip())
        self.cur = self.pad * level
        self.level = level

    def put(self, text, space=True):
        if space and self.cur.strip():
            self.cur += " "
        self.cur += text

    def finish(self):
        if self.cur.strip():
            self.lines.append(self.cur.rstrip())
        self.cur = ""
        return self.lines


# ---------------------------------------------------------------------------
# 포매팅
# ---------------------------------------------------------------------------

def _need_space(prev_kind, prev_text, text):
    if prev_kind is None:
        return False
    if text in NO_SPACE_BEFORE or prev_text in NO_SPACE_AFTER:
        return False
    if text == "(" and prev_kind == "word":
        # 함수 호출(COUNT(...))은 붙이고, 키워드(IN (...))는 띄운다
        return prev_text.upper() in SPACE_BEFORE_PAREN
    return True


def _display(kind, text, upper):
    if not upper:
        return text
    if kind == "kw":
        return text.upper()
    if kind == "word" and (text.upper() in KEYWORDS or text.upper() in FUNCTIONS):
        return text.upper()
    return text


def _is_exception(u, prev_u):
    """절 키워드처럼 보이지만 실제로는 일반 구문인 경우 (IS DISTINCT FROM, TIMESTAMP WITH TIME ZONE)."""
    if u == "FROM" and prev_u == "DISTINCT":
        return True
    if u == "WITH" and prev_u in ("TIME", "TIMESTAMP"):
        return True
    return False


def _next_sig(tokens, idx):
    for j in range(idx + 1, len(tokens)):
        if tokens[j][0] != "comment":
            return tokens[j]
    return (None, None)


def _signature(sql):
    """공백을 제외한 토큰 시퀀스 (키워드는 대소문자 무시). 포맷 전후 비교용."""
    return [(k, t.upper() if k == "word" else t) for k, t in tokenize(sql)]


def format_sql(sql, indent=2, upper=True):
    tokens = _merge(tokenize(sql))
    out = _Writer(" " * indent)

    frames = []        # 괄호/CASE 스택: {"kind", "clause", "cond", "base", "line"}
    base = 0           # 현재 절 키워드의 들여쓰기 단계
    clause = None      # 현재 절 (SELECT, FROM, WHERE ...)
    cond = None        # AND/OR를 줄바꿈할 때 쓰는 들여쓰기 단계
    pending = None     # 다음 토큰 앞에서 줄을 바꿀 단계
    between = False    # BETWEEN ... AND 의 AND 판별용
    prev_kind = prev_text = prev_u = None

    for idx, (kind, text) in enumerate(tokens):
        # ---- 주석 ----
        if kind == "comment":
            if text.startswith("--"):
                if out.cur.strip():
                    out.newline(out.level)
                out.put(text, False)
                out.newline(out.level)
            else:
                out.put(text, True)
            continue

        u = text.upper()
        is_word = kind in ("word", "kw")
        top = frames[-1]["kind"] if frames else None
        in_fn = top == "fn"
        sp = _need_space(prev_kind, prev_text, text)
        disp = _display(kind, text, upper)

        # 절 뒤의 첫 항목을 다음 줄로 내림 (단, SELECT DISTINCT / SELECT ALL 은 예외)
        if pending is not None and not (prev_u in ("SELECT", "DISTINCT") and u in ("DISTINCT", "ALL")):
            out.newline(pending)
            pending = None

        if is_word and not in_fn and u in BLOCK and not _is_exception(u, prev_u):
            out.newline(base)
            out.put(disp, sp)
            clause = u
            cond = base + 1 if u in ("WHERE", "HAVING") else None
            pending = base + 1 if u in BREAK_AFTER else None

        elif is_word and u == "CASE":
            out.put(disp, sp)
            frames.append({"kind": "case", "clause": clause, "cond": cond,
                           "base": base, "line": out.level})
            cond = None

        elif is_word and u in ("WHEN", "ELSE") and top == "case":
            out.newline(frames[-1]["line"] + 1)
            out.put(disp, sp)

        elif is_word and u == "END" and top == "case":
            f = frames.pop()
            out.newline(f["line"])
            out.put(disp, sp)
            clause, cond = f["clause"], f["cond"]

        elif is_word and u == "BETWEEN":
            between = True
            out.put(disp, sp)

        elif is_word and u == "AND" and between:
            between = False
            out.put(disp, sp)

        elif is_word and u in ("AND", "OR") and not in_fn and cond is not None:
            out.newline(cond)
            out.put(disp, sp)

        elif is_word and u.endswith("JOIN") and clause == "FROM" and not in_fn:
            out.newline(base + 1)
            out.put(disp, sp)
            cond = None

        elif is_word and u == "ON" and clause == "FROM" and not in_fn:
            out.newline(base + 2)
            out.put(disp, sp)
            cond = base + 3

        elif kind == "op" and text == "(":
            nk, nt = _next_sig(tokens, idx)
            is_sub = nk == "word" and nt.upper() in ("SELECT", "WITH", "VALUES")
            out.put("(", sp)
            if is_sub:
                frames.append({"kind": "sub", "clause": clause, "cond": cond,
                               "base": base, "line": out.level})
                base = out.level + 1
                clause, cond = None, None
                out.newline(base)
            else:
                frames.append({"kind": "fn", "clause": clause, "cond": cond,
                               "base": base, "line": out.level})
                cond = None

        elif kind == "op" and text == ")":
            if frames:
                f = frames.pop()
                if f["kind"] == "sub":
                    out.newline(f["line"])
                out.put(")", False)
                clause, cond, base = f["clause"], f["cond"], f["base"]
            else:
                out.put(")", False)

        elif kind == "op" and text == ",":
            out.put(",", False)
            if not in_fn and top != "case" and clause in COMMA_CLAUSES:
                out.newline(base if clause == "WITH" else base + 1)

        elif kind == "op" and text == ";":
            out.put(";", False)
            out.newline(0)
            out.lines.append("")  # 문장 사이 빈 줄
            frames, clause, cond, pending, between, base = [], None, None, None, False, 0
            prev_kind = prev_text = prev_u = None
            continue

        else:
            out.put(disp, sp)

        prev_kind, prev_text, prev_u = kind, text, u

    lines = out.finish()
    result = "\n".join(lines).strip("\n")
    result = result + "\n" if result else ""

    # 안전장치: 토큰이 바뀌었으면 결과를 버린다
    if _signature(sql) != _signature(result):
        raise ValueError("포맷 후 토큰이 달라졌습니다. 원본 쿼리를 보존하기 위해 중단합니다.")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="PostgreSQL 쿼리 포매터")
    ap.add_argument("input", nargs="?", help="SQL 파일 경로 (생략하면 stdin에서 읽음)")
    ap.add_argument("-o", "--output", help="결과를 저장할 파일 (생략하면 stdout)")
    ap.add_argument("-i", "--indent", type=int, default=2, help="들여쓰기 칸 수 (기본 2)")
    ap.add_argument("--keep-case", action="store_true", help="키워드 대소문자를 그대로 둠")
    ap.add_argument("--check", action="store_true", help="포맷이 필요하면 종료코드 1 반환 (CI용)")
    args = ap.parse_args(argv)

    if args.input:
        with open(args.input, encoding="utf-8") as f:
            src = f.read()
    else:
        src = sys.stdin.read()

    try:
        result = format_sql(src, indent=args.indent, upper=not args.keep_case)
    except ValueError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2

    if args.check:
        if result != src:
            print("포맷이 필요합니다.", file=sys.stderr)
            return 1
        return 0

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
        sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
