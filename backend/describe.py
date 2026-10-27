"""테이블 · 컬럼 설명 자동 채우기와 테이블 관계 추론.

설명: 대상 DB 코멘트가 있으면 그대로 쓴다. 없으면 LLM 이 이름 · 타입 · 샘플 값 몇 행을 보고 추론해 ai_comment 에 둔다.
      동기화는 ai_comment 를 건드리지 않고, 계약서(contract.py)는 코멘트가 비었을 때 ai_comment 를 쓴다.
관계: FK 가 없는 DB 를 위해 컬럼 이름 · 타입 · PK 로 조인 관계를 추론해 source='auto' 로 둔다.
      다시 돌리면 auto 관계를 통째로 새로 만든다. FK · 사람이 등록한 관계와 겹치는 것은 만들지 않는다.
"""

import datetime
import decimal
import json
import logging
from uuid import UUID, uuid4

from langchain_core.messages import HumanMessage, SystemMessage

import background
import db
from compiler import Dialect, type_family
from contract import load_tables
from metrics import read_only
from models import ApiException, DescribeRequest, DescribeRun
from selector import parse_json
from systems import connection_row

log = logging.getLogger("nl2sql.describe")

SAMPLE_ROWS = 5
SAMPLE_TIMEOUT_S = 15
VALUE_CHARS = 40  # 샘플 값 하나를 프롬프트에 실을 때 자르는 길이
DESC_CHARS = 200  # 추론한 설명 하나의 최대 길이

# 이 이름의 PK 는 어느 테이블에나 있어 이름만으로 가리키는 테이블을 알 수 없다 — '<테이블>_id' 처럼 적힌 컬럼만 잇는다
GENERIC_KEYS = {"id", "seq", "no", "idx", "sn", "key", "code", "cd"}
KEY_SUFFIXES = ("_id", "_no", "_seq", "_sn", "_idx", "_key", "_code", "_cd")

SYSTEM_PROMPT = """너는 데이터베이스 메타데이터 작성자다.
테이블 하나의 이름, 컬럼 이름 · 타입, 이미 있는 설명, 샘플 값 몇 행을 보고 '설명이 필요한 것'만 한국어로 쓴다.

규칙:
- 설명은 한 문장, 40자 안팎. 무엇을 담는지 쓴다 (예: "차량 식별 번호", "경고 발생 시각 (UTC)", "운행 상태 코드 (R=운행, S=정지)").
- 코드 값 컬럼은 샘플에 보이는 값의 뜻을 짐작해 괄호에 적는다. 확실하지 않은 값의 뜻은 지어내지 않는다.
- 단위 · 시간대가 이름이나 값에서 드러나면 적는다.
- 이름 · 값으로 뜻을 전혀 알 수 없으면 빈 문자열로 둔다.
- 설명이 필요하다고 한 것만 답한다.

답은 JSON 객체 하나: {"table": "테이블 설명 (필요할 때만)", "columns": {"컬럼 이름": "설명"}}"""


# ── 실행 ──────────────────────────────────────────────────────────────────


def start_describe(system_id: UUID, req: DescribeRequest, model) -> DescribeRun:
    """실행 기록을 만들고 스레드에서 돌린다. 진행은 get_describe_run 으로 본다."""
    if not req.descriptions and not req.relations:
        raise ApiException(400, "설명과 관계 중 하나는 골라야 합니다")
    targets = []
    if req.descriptions:
        targets = describe_targets(system_id, req.table_ids, req.overwrite)
    run_id = uuid4()
    options = {"descriptions": req.descriptions, "overwrite": req.overwrite, "relations": req.relations,
               "picked": "manual" if req.table_ids else "auto"}
    _execute(
        "INSERT INTO meta_describe_run (id, system_id, options, tables_total) VALUES (%s, %s, %s, %s)",
        str(run_id),
        str(system_id),
        json.dumps(options, ensure_ascii=False),
        len(targets),
    )

    def work():
        run_describe(system_id, run_id, targets, req.relations, model)

    def on_error(error):
        _close(run_id, "error", _first_line(error))

    if not background.start(f"describe:{system_id}", run_id, work, on_error):
        _execute("DELETE FROM meta_describe_run WHERE id = %s", str(run_id))
        raise ApiException(409, "이 시스템에서 설명 자동 채우기가 이미 돌고 있습니다")
    log.info("설명 자동 채우기 시작: run=%s tables=%d relations=%s", run_id, len(targets), req.relations)
    return get_describe_run(system_id, run_id)


def run_describe(system_id: UUID, run_id: UUID, targets: list[dict], relations: bool, model) -> None:
    if relations:
        added = infer_and_store_relations(system_id)
        _execute("UPDATE meta_describe_run SET relations_added = %s WHERE id = %s", added, str(run_id))
    if targets:
        row = connection_row(system_id)
        d = Dialect(row["driver"])
        for target in targets:
            try:
                filled_table, filled_columns = describe_table(row, d, target, model)
                _execute(
                    """
                    UPDATE meta_describe_run SET tables_done = tables_done + 1,
                           tables_filled = tables_filled + %s, columns_filled = columns_filled + %s
                     WHERE id = %s
                """,
                    1 if filled_table else 0,
                    filled_columns,
                    str(run_id),
                )
            except Exception as error:
                log.warning("%s 설명을 채우지 못했습니다: %s", target["name"], _first_line(error))
                failure = json.dumps([{"table": target["name"], "error": _first_line(error)}], ensure_ascii=False)
                _execute(
                    "UPDATE meta_describe_run SET tables_done = tables_done + 1, failures = failures || %s::jsonb"
                    " WHERE id = %s",
                    failure,
                    str(run_id),
                )
    _close(run_id, "done", None)
    log.info("설명 자동 채우기 끝: run=%s", run_id)


def list_describe_runs(system_id: UUID, limit: int = 50) -> list[DescribeRun]:
    runs = []
    for r in db.query(
        "SELECT * FROM meta_describe_run WHERE system_id = %s ORDER BY started_at DESC LIMIT %s",
        str(system_id),
        limit,
    ):
        runs.append(_row_to_run(r))
    return runs


def get_describe_run(system_id: UUID, run_id: UUID) -> DescribeRun:
    row = db.one("SELECT * FROM meta_describe_run WHERE id = %s AND system_id = %s", str(run_id), str(system_id))
    if row is None:
        raise ApiException(404, "설명 자동 채우기 실행을 찾을 수 없습니다")
    return _row_to_run(row)


# ── 설명 ──────────────────────────────────────────────────────────────────


def describe_targets(system_id: UUID, table_ids: list[UUID], overwrite: bool) -> list[dict]:
    """설명을 채울 테이블 → [{id, name, schema, comment, need_table, columns: [{name, type, comment, need}]}]

    대상 DB 코멘트가 있는 것은 채우지 않는다. 전에 추론한 설명은 overwrite 일 때만 다시 채운다.
    """
    rows = db.query(
        """
        SELECT t.id AS table_id, t.name AS table_name, t.db_schema, t.comment AS table_comment,
               t.ai_comment AS table_ai, c.name AS column_name, c.data_type, c.comment, c.ai_comment, c.note, c.is_pk
          FROM meta_table t
          JOIN meta_column c ON c.table_id = t.id AND c.deleted_at IS NULL
         WHERE t.system_id = %s AND t.deleted_at IS NULL
         ORDER BY t.name, c.ordinal
    """,
        str(system_id),
    )
    picked = set()
    for tid in table_ids:
        picked.add(str(tid))
    tables: dict = {}
    for r in rows:
        tid = str(r["table_id"])
        if picked and tid not in picked:
            continue
        table = tables.get(tid)
        if table is None:
            table = {
                "id": tid,
                "name": r["table_name"],
                "schema": r["db_schema"],
                "comment": r["table_comment"],
                "need_table": _needs(r["table_comment"], r["table_ai"], overwrite),
                "columns": [],
            }
            tables[tid] = table
        table["columns"].append(
            {
                "name": r["column_name"],
                "type": r["data_type"],
                "comment": r["note"] or r["comment"],  # 사람이 적은 설명이 있으면 채우지 않는다
                "is_pk": r["is_pk"],
                "need": _needs(r["note"] or r["comment"], r["ai_comment"], overwrite),
            }
        )
    if picked:
        for tid in picked:
            if tid not in tables:
                raise ApiException(404, f"테이블을 찾을 수 없습니다: {tid}")
    out = []
    for table in tables.values():
        if table["need_table"] or _needed_columns(table):
            out.append(table)
    return out


def describe_table(row: dict, d: Dialect, table: dict, model) -> tuple[bool, int]:
    """테이블 하나의 빈 설명을 추론해 저장한다 → (테이블 설명을 채웠나, 채운 컬럼 수)"""
    samples, note = _samples(row, d, table)
    asked = _needed_columns(table)
    messages = [SystemMessage(SYSTEM_PROMPT), HumanMessage(build_prompt(row, table, asked, samples, note))]
    reply = model.invoke(messages)
    try:
        answer = parse_json(reply.content)
    except ValueError as error:
        messages = messages + [reply, HumanMessage(f"형식이 틀렸습니다: {error}. 설명 없이 JSON 객체 하나만 다시 답하십시오.")]
        answer = parse_json(model.invoke(messages).content)
    table_desc, column_descs = read_answer(answer, table["need_table"], asked)
    _store(table["id"], table_desc, column_descs)
    log.info("%s 설명: 테이블 %s, 컬럼 %d/%d", table["name"], "채움" if table_desc else "-", len(column_descs), len(asked))
    return bool(table_desc), len(column_descs)


def build_prompt(row: dict, table: dict, asked: list[str], samples: list[list], note: str) -> str:
    lines = []
    domain = " ".join(str(row.get("domain_desc") or "").split())
    if domain:
        lines.append(f"업무 영역: {domain}")
    lines.append(f"테이블: {table['name']}")
    if table["comment"]:
        lines.append(f"테이블 설명: {table['comment']}")
    lines.append(f"테이블 설명 필요: {'예' if table['need_table'] else '아니오'}")
    lines.append("")
    lines.append("컬럼 (이름 | 타입 | 설명):")
    for c in table["columns"]:
        if c["need"]:
            desc = "(설명 필요)"
        else:
            desc = c["comment"] or "(설명 필요 없음)"
        pk = " PK" if c["is_pk"] else ""
        lines.append(f"- {c['name']} | {c['type']}{pk} | {desc}")
    lines.append("")
    if samples:
        names = []
        for c in table["columns"]:
            names.append(c["name"])
        lines.append(f"샘플 {len(samples)}행 (컬럼 순서: {', '.join(names)}):")
        for values in samples:
            lines.append(json.dumps(values, ensure_ascii=False))
    else:
        lines.append(f"샘플 값 없음 ({note})")
    lines.append("")
    lines.append(f"설명이 필요한 컬럼: {', '.join(asked) if asked else '없음'}")
    return "\n".join(lines)


def read_answer(answer: dict, need_table: bool, asked: list[str]) -> tuple[str, dict]:
    """모델 답 → (테이블 설명, {컬럼: 설명}). 묻지 않은 것과 빈 설명은 버린다."""
    table_desc = ""
    if need_table:
        table_desc = _clean(answer.get("table"))
    wanted = {}
    for name in asked:
        wanted[name.lower()] = name
    columns = {}
    given = answer.get("columns")
    if isinstance(given, dict):
        for key, value in given.items():
            name = wanted.get(str(key).lower())
            text = _clean(value)
            if name and text:
                columns[name] = text
    return table_desc, columns


def _samples(row: dict, d: Dialect, table: dict) -> tuple[list[list], str]:
    """앞에서 몇 행. 읽지 못해도 설명은 이름 · 타입으로 추론한다 — 그때 이유를 돌려준다."""
    quoted = []
    for c in table["columns"]:
        quoted.append(d.q(c["name"]))
    sql = f"SELECT {', '.join(quoted)} FROM {d.table(table['name'], table['schema'])} {d.limit(SAMPLE_ROWS)}"
    try:
        with read_only(row, SAMPLE_TIMEOUT_S) as cur:
            cur.execute(sql)
            fetched = cur.fetchall()
    except Exception as error:
        return [], f"샘플을 읽지 못했습니다: {_first_line(error)}"
    out = []
    for r in fetched:
        values = []
        for v in r:
            values.append(_sample_value(v))
        out.append(values)
    return out, "" if out else "빈 테이블"


def _sample_value(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<binary>"
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    text = str(value)
    return text if len(text) <= VALUE_CHARS else text[: VALUE_CHARS - 3] + "..."


def _store(table_id: str, table_desc: str, column_descs: dict) -> None:
    with db.connection() as conn:
        with conn.cursor() as cur:
            if table_desc:
                cur.execute(
                    "UPDATE meta_table SET ai_comment = %s, updated_at = now() WHERE id = %s", (table_desc, table_id)
                )
            rows = []
            for name, text in column_descs.items():
                rows.append((text, table_id, name))
            if rows:
                cur.executemany(
                    "UPDATE meta_column SET ai_comment = %s, updated_at = now() WHERE table_id = %s AND name = %s", rows
                )


def _needs(comment: str, ai_comment: str, overwrite: bool) -> bool:
    if (comment or "").strip():
        return False
    return overwrite or not (ai_comment or "").strip()


def _needed_columns(table: dict) -> list[str]:
    out = []
    for c in table["columns"]:
        if c["need"]:
            out.append(c["name"])
    return out


def _clean(value) -> str:
    text = " ".join(str(value or "").split())
    return text[:DESC_CHARS]


# ── 관계 ──────────────────────────────────────────────────────────────────


def infer_and_store_relations(system_id: UUID) -> int:
    """auto 관계를 새로 추론해 바꿔 넣는다 → 넣은 수"""
    tables = load_tables(system_id)
    existing = set()
    for r in db.query(
        """
        SELECT lt.name AS left_table, r.left_column, rt.name AS right_table, r.right_column
          FROM meta_relation r
          JOIN meta_table lt ON lt.id = r.left_table_id
          JOIN meta_table rt ON rt.id = r.right_table_id
         WHERE r.system_id = %s AND r.source <> 'auto'
    """,
        str(system_id),
    ):
        existing.add(_pair_key(r["left_table"], r["left_column"], r["right_table"], r["right_column"]))
    rows = []
    for r in infer_relations(list(tables.values())):
        if _pair_key(r["left_table"], r["left_column"], r["right_table"], r["right_column"]) in existing:
            continue
        rows.append(
            (
                str(uuid4()),
                str(system_id),
                f"auto:{r['left_table']}.{r['left_column']}->{r['right_table']}.{r['right_column']}",
                tables[r["left_table"]]["id"],
                r["left_column"],
                tables[r["right_table"]]["id"],
                r["right_column"],
            )
        )
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM meta_relation WHERE system_id = %s AND source = 'auto'", (str(system_id),))
            if rows:
                cur.executemany(
                    """
                    INSERT INTO meta_relation (id, system_id, constraint_name, left_table_id, left_column,
                                               right_table_id, right_column, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'auto')
                """,
                    rows,
                )
    log.info("system %s: 관계 %d개 추론", system_id, len(rows))
    return len(rows)


def infer_relations(tables: list[dict]) -> list[dict]:
    """컬럼 이름 · 타입 · PK 로 관계를 추론한다 → [{left_table, left_column, right_table, right_column}]

    PK 가 컬럼 하나인 테이블만 가리킬 수 있다.
    - PK 이름이 고유하면 (vehicle_id): 다른 테이블의 같은 이름 컬럼이 그 PK 를 가리킨다.
      같은 PK 이름인 테이블이 여럿이면 이름이 맞는 테이블(vehicle, tb_vehicle)만 남긴다.
    - PK 이름이 흔하면 (id): '<테이블>_id' 처럼 적힌 컬럼만 그 테이블을 가리킨다.
    타입 갈래(숫자 · 문자 · 시각)가 다르면 잇지 않는다.
    """
    pk_of = {}
    for t in tables:
        keys = []
        for c in t["columns"]:
            if c.get("is_pk"):
                keys.append(c)
        if len(keys) == 1:
            pk_of[t["name"]] = keys[0]

    out, seen = [], set()
    for t in tables:
        own_pk = pk_of.get(t["name"])
        for c in t["columns"]:
            column = c["name"].lower()
            # 자기 PK 이고 테이블 이름도 맞으면 이 테이블이 그 키의 본 테이블이다 (vehicle.vehicle_id) — 가리키지 않는다
            if own_pk is c and _names_match(t["name"], _stem(column)):
                continue
            target = _target(column, t["name"], pk_of)
            if target is None:
                continue
            # PK 끼리는 이름이 맞는 본 테이블일 때만 잇는다 (vehicle_ext.vehicle_id → vehicle)
            if own_pk is c and not _names_match(target, _stem(column)):
                continue
            pk = pk_of[target]
            if type_family(c.get("type", "")) != type_family(pk.get("type", "")):
                continue
            key = _pair_key(t["name"], c["name"], target, pk["name"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"left_table": t["name"], "left_column": c["name"], "right_table": target,
                        "right_column": pk["name"]})
    return out


def _target(column: str, own: str, pk_of: dict) -> str | None:
    """컬럼이 가리키는 테이블. 없거나 여럿이면 None"""
    stem = _stem(column)
    candidates = []
    for name, pk in pk_of.items():
        if name == own:
            continue
        key = pk["name"].lower()
        if key in GENERIC_KEYS:
            # vehicle_id → vehicle.id : 컬럼이 '<테이블>_<PK>' 꼴일 때만
            if column.endswith("_" + key) and _names_match(name, column[: -len(key) - 1]):
                candidates.append(name)
        elif key == column:
            # 키 꼴(_id, _no …)이 아닌 PK (name, company)는 흔한 단어라 테이블 이름이 맞을 때만
            if _stem(key) or _names_match(name, key):
                candidates.append(name)
    if len(candidates) > 1 and stem:
        named = []
        for name in candidates:
            if _names_match(name, stem):
                named.append(name)
        candidates = named
    return candidates[0] if len(candidates) == 1 else None


def _names_match(table: str, stem: str) -> bool:
    """테이블 이름(스키마를 뗀)이 stem 이거나 접두어 + stem 인가 (vehicle, tb_vehicle, t_vehicle)"""
    bare = table.rsplit(".", 1)[-1].lower()
    return bool(stem) and (bare == stem or bare.endswith("_" + stem) or bare == stem + "s")


def _stem(column: str) -> str:
    for suffix in KEY_SUFFIXES:
        if column.endswith(suffix) and len(column) > len(suffix):
            return column[: -len(suffix)]
    return ""


def _pair_key(left_table: str, left_column: str, right_table: str, right_column: str) -> frozenset:
    """방향 없이 같은 관계인지 본다"""
    return frozenset([(left_table, left_column), (right_table, right_column)])


# ── 기록 ──────────────────────────────────────────────────────────────────


def _close(run_id: UUID, status: str, error: str | None) -> None:
    _execute(
        "UPDATE meta_describe_run SET status = %s, error = %s, finished_at = now() WHERE id = %s",
        status,
        error,
        str(run_id),
    )


def _execute(query: str, *args) -> None:
    with db.connection() as conn:
        conn.execute(query, args)


def _row_to_run(r: dict) -> DescribeRun:
    status = r["status"]
    if status == "running" and not background.is_active(r["id"]):
        status = "interrupted"  # 서버가 재시작돼 멈춘 실행
    return DescribeRun(
        id=r["id"],
        system_id=r["system_id"],
        status=status,
        options=r["options"] or {},
        tables_total=r["tables_total"],
        tables_done=r["tables_done"],
        tables_filled=r["tables_filled"],
        columns_filled=r["columns_filled"],
        relations_added=r["relations_added"],
        failures=r["failures"] or [],
        error=r["error"],
        started_at=r["started_at"].isoformat(),
        finished_at=r["finished_at"].isoformat() if r["finished_at"] else None,
    )


def _first_line(error) -> str:
    lines = str(getattr(error, "detail", None) or error).strip().splitlines()
    return lines[0] if lines else type(error).__name__
