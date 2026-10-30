"""지표 자동 등록: 테이블마다 LLM 이 지표 후보를 쓰고, 등록 확인을 통과한 것만 지표로 저장한다.

후보 하나는 지표 등록과 같은 확인(AST 변환 → 컴파일 → 실행 전 확인 → 결과 비교)을 거치고,
실패하면 오류를 붙여 그 후보만 다시 쓰게 한다. 등록됐든 아니든 후보마다 시도와 이유를 남긴다 (metric_auto_item).
저장은 지표 등록(metrics.create_metric)을 그대로 부른다 — 사람이 등록한 지표와 같은 길이다.
"""

import datetime
import json
import logging
from uuid import UUID, uuid4

from langchain_core.messages import HumanMessage, SystemMessage

import background
import db
from catalog import read_indexes
from compiler import AGGREGATES, Dialect, temporal_kind
from contract import load_contract
from metric_draft import DRAFT_DAYS, full_scans, positional_refs
from metrics import STAGE_LABEL, check_metric, create_metric, read_only
from models import ApiException, AutoItem, AutoRun, AutoRunDetail, AutoRunRequest, AutoTry, MetricCheck, MetricInput
from selector import Step, chat, model_name, parse_json
from sql_ast import parse_sql
from systems import connection_row

log = logging.getLogger("nl2sql.auto")

MAX_ATTEMPTS = 3  # 후보 하나를 고쳐 쓰는 최대 횟수
PLAN_TIMEOUT_S = 10
MAX_RELATED = 12  # 프롬프트에 싣는 이어진 테이블 수
MAX_RELATED_COLUMNS = 40
MAX_OTHER_METRICS = 100

AUTO_SYSTEM = """당신은 '{name}' 데이터베이스({driver})에 등록할 업무 지표를 찾아 SQL 로 쓰는 역할입니다.
지표는 계산이 필요한 숫자입니다. 등록된 지표는 질문마다 다른 LLM 이 설명을 읽고 고른 뒤, SQL 의 기간 · 조건 · 묶음만 바꿔 씁니다.
{domain}
[규칙]
- [대상 테이블] 로 계산하는 지표를 최대 {count}개 고릅니다. 등록할 지표는 질문을 받은 LLM 이 테이블과 컬럼만 보고는 바로 만들기 어려운 계산입니다:
  1) [계산 필요] 컬럼을 계산하는 것 (예: 누적값의 하루 MAX - MIN)
  2) 업무 규칙이 들어간 조건이나 분류 (예: 특정 코드 값만 세기, CASE 로 구간 나누기)
  3) 분자와 분모가 정해진 비율, 여러 집계를 섞은 식 (예: 경고 차량 수 / 전체 차량 수)
  4) 여러 테이블의 값을 함께 써야 정의되는 계산 (예: 판매 수량 × 상품 원가, 반품 금액 / 판매 금액)
- 한 컬럼을 그대로 COUNT · SUM · AVG · MIN · MAX 하고 컬럼이나 날짜로 묶기만 하는 단순 집계는 쓰지 않습니다 (질문 LLM 이 바로 만듭니다).
- [관계] 가 있으면 후보 중 하나 이상은 이어진 테이블을 JOIN 해야 정의되는 지표로 고릅니다.
- 위에 맞는 계산이 없으면 억지로 만들지 않습니다. 개수를 채우지 못해도 됩니다.
- [이미 있는 지표] 와 같은 계산은 쓰지 않습니다. 이름도 겹치지 않게 합니다.
- 지표 하나는 계산 하나입니다. 개수 · 정렬 · 특정 값 조건은 넣지 않습니다 (질문마다 붙습니다).
{period_rule}
- 묶음 단위를 하나 정해 GROUP BY 합니다 (예: 날짜별, 매장별). 묶음이 없으면 전체 한 행입니다.
- 다른 테이블은 [관계] 의 컬럼으로만 JOIN 합니다. 같은 테이블을 두 번 쓰지 않습니다 (셀프 조인은 쓸 수 없습니다).
- JOIN 으로 행이 불어나 합계가 부풀지 않게 합니다. 팩트 테이블끼리 바로 JOIN 하지 않습니다.
- 비율은 SUM(a) * 1.0 / NULLIF(SUM(b), 0) 처럼 적어 0 나누기와 정수 나눗셈을 막습니다.
- [계산 필요] 컬럼은 그대로 SUM · AVG 하지 않습니다.
- [UTC +n시간] 컬럼은 UTC 로 저장된 시각입니다. 현지 날짜로 묶을 때는 '컬럼 + n시간' 으로 적습니다.
- 쓸 수 있는 문법: SELECT 문 하나, INNER · LEFT JOIN (ON a.x = b.x), WHERE, GROUP BY, HAVING, ORDER BY, LIMIT,
  SUM · AVG · MIN · MAX · COUNT · COUNT(DISTINCT 컬럼), CASE, COALESCE, NULLIF, ROUND, ABS, 사칙연산, 날짜로 자르기(DATE · DATE_TRUNC), ROW_NUMBER.
  쓸 수 없는 것: UNION, 하위 쿼리 JOIN, IN (하위 쿼리), DISTINCT ON, OFFSET, 날짜가 아닌 형 변환(CAST), 윈도 범위(ROWS · RANGE).
- GROUP BY · ORDER BY 에 순번(1, 2)을 쓰지 않습니다. SELECT 결과 컬럼마다 별칭(AS)을 붙입니다.
- description 에는 무엇을 어떻게 계산하는지(쓰는 컬럼, 묶음 단위, 빼는 행) 적습니다. 질문을 받은 LLM 이 이 지표가 맞는지 판단할 때 읽습니다.
- rationale 에는 이 지표를 고른 이유를 한 문장으로 적습니다.
- 지표로 삼을 계산이 없으면 metrics 를 빈 목록으로 두고 skip 에 이유를 적습니다.
- 설명 없이 JSON 객체 하나로만 답합니다:
  {{"metrics": [{{"name": "지표 이름", "description": "계산 설명", "synonyms": ["같은 말"], "sql": "SELECT ...", "rationale": "고른 이유"}}], "skip": ""}}"""

FIX_REQUEST = """지표 '{name}' 의 SQL 이 {stage}: {error}
이 지표 하나만 고쳐 설명 없이 JSON 객체 하나로 다시 답하십시오:
{{"name": "지표 이름", "description": "계산 설명", "synonyms": ["같은 말"], "sql": "SELECT ...", "rationale": "고른 이유", "unsure": ""}}
고칠 수 없으면 sql 을 빈 문자열로 두고 unsure 에 이유를 적으십시오."""

# 예시 기간이 있으면 지표 초안처럼 인덱스 시각 컬럼에 건다 — 질문마다 다른 LLM 이 이 기간을 바꾼다.
# 없으면 기간을 넣지 않고, 큰 테이블 전체를 훑는지는 실행 전 확인이 판단한다.
PERIOD_RULE = (
    "- 기간 조건은 [기간 예시] 대로 {table}.{column} 에 적습니다. 이 컬럼은 인덱스 앞 칸이라 WHERE 에서 함수나 계산으로 감싸지 않습니다 (인덱스를 타게).\n"
    "  예시 기간입니다 — 질문마다 다른 LLM 이 이 기간을 바꿉니다. 결과 비교를 빨리 끝내려고 짧게 둡니다."
)
NO_PERIOD_RULE = "- 기간 조건은 넣지 않습니다 (질문마다 붙습니다)."


# ── 시작 · 조회 ───────────────────────────────────────────────────────────


def start_run(system_id: UUID, req: AutoRunRequest, model) -> AutoRun:
    """실행 기록을 만들고 스레드에서 돌린다. 진행은 get_run 으로 본다."""
    contract = load_contract(system_id)
    tables = pick_tables(contract, req)
    if not tables:
        raise ApiException(400, "지표 후보를 쓸 테이블이 없습니다 — 모든 테이블에 지표가 있습니다. 테이블을 직접 고르십시오")
    run_id = uuid4()
    options = {"per_table": req.per_table, "allow_full_scan": req.allow_full_scan, "picked": "manual" if req.tables else "auto"}
    _execute(
        "INSERT INTO metric_auto_run (id, system_id, options, tables) VALUES (%s, %s, %s, %s)",
        str(run_id),
        str(system_id),
        json.dumps(options, ensure_ascii=False),
        json.dumps(tables, ensure_ascii=False),
    )

    def work():
        run_auto(system_id, run_id, tables, req, model)

    def on_error(error):
        _close(run_id, "error", _first_line(error))

    if not background.start(f"auto:{system_id}", run_id, work, on_error):
        _execute("DELETE FROM metric_auto_run WHERE id = %s", str(run_id))
        raise ApiException(409, "이 시스템에서 지표 자동 등록이 이미 돌고 있습니다")
    log.info("지표 자동 등록 시작: run=%s tables=%s", run_id, tables)
    return get_run(system_id, run_id)


def list_runs(system_id: UUID, limit: int = 50) -> list[AutoRun]:
    runs = []
    for r in db.query(
        "SELECT * FROM metric_auto_run WHERE system_id = %s ORDER BY started_at DESC LIMIT %s",
        str(system_id),
        limit,
    ):
        runs.append(_row_to_run(r))
    return runs


def get_run(system_id: UUID, run_id: UUID) -> AutoRunDetail:
    row = db.one("SELECT * FROM metric_auto_run WHERE id = %s AND system_id = %s", str(run_id), str(system_id))
    if row is None:
        raise ApiException(404, "자동 등록 실행을 찾을 수 없습니다")
    items = []
    for r in db.query("SELECT * FROM metric_auto_item WHERE run_id = %s ORDER BY id", str(run_id)):
        items.append(_row_to_item(r))
    return AutoRunDetail(**_row_to_run(row).model_dump(), items=items)


def pick_tables(contract: dict, req: AutoRunRequest) -> list[str]:
    """고른 테이블을 실제 이름으로. 고르지 않았으면 지표가 없는 테이블 중 나가는 관계(FK)가 많은 것부터 —
    관계가 많은 테이블은 대개 차원을 여럿 거느린 팩트 테이블이라 계산할 숫자가 있다."""
    names = {}
    for t in contract["tables"]:
        names[t["name"].lower()] = t["name"]
    if req.tables:
        picked = []
        for n in req.tables:
            real = names.get(n.lower())
            if real is None:
                raise ApiException(400, f"메타데이터에 없는 테이블입니다 -> {n}")
            if real not in picked:
                picked.append(real)
        return picked
    with_metric = set()
    for m in contract.get("metrics") or []:
        for n in m.get("tables") or []:
            with_metric.add(n)
    outgoing = {}
    for r in contract.get("relations") or []:
        outgoing[r["left_table"]] = outgoing.get(r["left_table"], 0) + 1
    candidates = []
    for t in contract["tables"]:
        if t["name"] not in with_metric:
            candidates.append(t["name"])
    candidates.sort(key=lambda n: (-outgoing.get(n, 0), n))
    return candidates[: req.max_tables]


# ── 실행 ──────────────────────────────────────────────────────────────────


def run_auto(system_id: UUID, run_id: UUID, tables: list[str], req: AutoRunRequest, model) -> None:
    row = connection_row(system_id)
    taken = set()
    for r in db.query("SELECT name FROM metric WHERE system_id = %s", str(system_id)):
        taken.add(r["name"].lower())
    prompt_tokens = completion_tokens = 0
    for done, name in enumerate(tables, start=1):
        # 테이블마다 다시 읽는다 — 앞 테이블에서 등록한 지표가 [이미 있는 지표] 에 실린다
        contract = load_contract(system_id)
        table = None
        for t in contract["tables"]:
            if t["name"] == name:
                table = t
        step = Step("지표 자동 등록", model=model_name(model))
        if table is None:
            _insert_item(run_id, _failed_item(name, "llm", "동기화 뒤 사라진 테이블입니다"))
        else:
            _table_run(system_id, run_id, row, contract, table, req, model, step, taken)
        prompt_tokens += step.prompt_tokens
        completion_tokens += step.completion_tokens
        _progress(run_id, done, prompt_tokens, completion_tokens)
    _close(run_id, "done", None)
    log.info("지표 자동 등록 끝: run=%s", run_id)


def _table_run(system_id, run_id, row, contract, table, req, model, step, taken) -> None:
    """테이블 하나: 후보를 받고 후보마다 등록을 시도한다"""
    indexes = _indexes(row, table)
    period = period_example(row, table, indexes)
    system, user = auto_prompt(contract, table, indexes, req.per_table, period)
    base = [SystemMessage(system), HumanMessage(user)]
    try:
        reply, parsed = _ask(model, base, step)
    except ValueError as error:
        _insert_item(run_id, _failed_item(table["name"], "llm", str(error)))
        return
    proposals = []
    for p in parsed.get("metrics") or []:
        if isinstance(p, dict):
            proposals.append(p)
    if not proposals:
        skip = str(parsed.get("skip") or "").strip() or "LLM 이 지표 후보를 내지 않았습니다"
        item = _failed_item(table["name"], "llm", skip)
        item["status"] = "skipped"
        _insert_item(run_id, item)
        return
    simple = _simple_check(contract, period)
    for p in proposals[: req.per_table]:
        item = _candidate(
            system_id, row, model, base + [reply], p, step, taken, table["name"], req.allow_full_scan, simple
        )
        _insert_item(run_id, item)


def _ask(model, messages: list, step: Step) -> tuple:
    """후보 목록을 받는다 → (응답 메시지, JSON). 형식이 틀리면 한 번 더 묻는다."""
    for attempt in (1, 2):
        reply = chat(model, messages, step, attempt)
        try:
            return reply, parse_json(reply.content)
        except ValueError as error:
            messages = messages + [reply, HumanMessage(f"형식이 틀렸습니다: {error}. 설명 없이 JSON 객체 하나만 다시 답하십시오.")]
            last = error
    raise ValueError(f"지표 후보 응답을 읽지 못했습니다: {last}")


def _candidate(system_id, row, model, messages, proposal, step, taken, table_name, allow_full_scan, simple=None) -> dict:
    """후보 하나를 등록까지 밀어 본다 → 기록할 항목. simple(sql) 이 이유를 돌려주면 단순 집계라 건너뛴다."""
    item = _failed_item(table_name, None, "")
    current = proposal
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _fill(item, current)
        item["attempts"] = attempt
        if not item["sql"]:
            item["stage"] = "llm"
            item["reason"] = str(current.get("unsure") or "").strip() or "SQL 이 비었습니다"
            return item
        if not item["name"]:
            item["stage"], item["reason"] = "llm", "지표 이름이 비었습니다"
            return item
        if item["name"].lower() in taken:
            item["status"], item["stage"] = "skipped", "duplicate"
            item["reason"] = f"'{item['name']}' 이름의 지표가 이미 있습니다"
            return item
        reason = simple(item["sql"]) if simple is not None else None
        if reason:
            # 대상 DB 에서 돌리기 전에 거른다. 지표가 아니라는 판단이라 고쳐 쓰게 하지 않는다
            item["status"], item["stage"], item["reason"] = "skipped", "simple", reason
            return item
        item["full_scans"] = []
        check = check_metric(system_id, item["sql"], guard=_guard(row, item, allow_full_scan))
        item["check"] = check
        item["tries"].append({"sql": item["sql"], "stage": check.stage, "error": check.error})
        if not check.error:
            break
        item["stage"], item["reason"] = check.stage, check.error
        if attempt == MAX_ATTEMPTS:
            return item
        stage = STAGE_LABEL.get(check.stage, "확인 실패")
        messages = messages + [HumanMessage(FIX_REQUEST.format(name=item["name"], stage=stage, error=check.error))]
        reply = chat(model, messages, step, attempt + 1)
        messages = messages + [reply]
        try:
            current = _one(parse_json(reply.content))
        except ValueError as error:
            item["stage"], item["reason"] = "llm", f"고친 응답을 읽지 못했습니다: {error}"
            return item

    try:
        metric = create_metric(
            system_id,
            MetricInput(name=item["name"], description=item["description"], synonyms=item["synonyms"], sql=item["sql"]),
        )
    except ApiException as error:
        item["stage"], item["reason"] = "save", str(error.detail)
        return item
    taken.add(item["name"].lower())
    item["status"], item["stage"], item["reason"] = "registered", None, ""
    item["metric_id"] = str(metric.id)
    log.info("지표 자동 등록: %s (%s, 시도 %d)", item["name"], table_name, item["attempts"])
    return item


SIMPLE_REASON = (
    "단순 집계입니다 — 한 컬럼을 그대로 COUNT · SUM · AVG 하고 묶기만 하는 계산은 질문할 때 LLM 이 바로 만들어 지표로 등록하지 않습니다"
)
_GROUPING_FUNCTIONS = {"DATE", "DATE_TRUNC", "FORMAT", "ADD_HOURS", "ADD_DAYS", "ADD_MINUTES"}


def _simple_check(contract: dict, period: dict | None):
    """후보 SQL 이 단순 집계인지 보는 함수 simple(sql) → 이유 또는 None. 옮기지 못하는 SQL 은 등록 확인에 맡긴다."""
    needs_calc = set()
    for t in contract["tables"]:
        for c in t["columns"]:
            if c.get("needs_calc"):
                needs_calc.add(c["name"].lower())
    period_column = period["column"].lower() if period else None

    def simple(sql: str) -> str | None:
        try:
            ast = parse_sql(sql, contract)["ast"]
        except ValueError:
            return None
        return SIMPLE_REASON if is_simple(ast, needs_calc, period_column) else None

    return simple


def is_simple(ast: dict, needs_calc: set, period_column: str | None) -> bool:
    """단순 집계: 집계마다 컬럼 하나(또는 COUNT(*))를 그대로 쓰고, 나머지는 컬럼이나 날짜 묶음이며,
    조건은 예시 기간과 NULL 여부뿐이다. JOIN 은 따지지 않는다 — 묶을 컬럼을 가져오는 JOIN 도 질문 LLM 이 만든다.
    계산 필요 컬럼의 집계, 집계끼리의 식, CASE, 값 조건(업무 규칙), HAVING, 하위 쿼리가 있으면 단순하지 않다."""
    if not isinstance(ast.get("from"), str) or ast.get("having"):
        return False
    for j in ast.get("joins") or []:
        if not isinstance(j.get("table"), str):
            return False
    aggregated = False
    for s in ast.get("select") or []:
        expr = s.get("expr") or {}
        if _plain_aggregate(expr, needs_calc):
            aggregated = True
        elif not _grouping(expr):
            return False
    for g in ast.get("group_by") or []:
        if not _grouping(g):
            return False
    for cond in _conditions(ast.get("where") or []):
        if cond.get("op") in ("is_null", "is_not_null"):
            continue
        column = _column_name(cond.get("expr") or {})
        if column is None or column != period_column:
            return False
    return aggregated


def _plain_aggregate(expr: dict, needs_calc: set) -> bool:
    if expr.get("fn") not in AGGREGATES:
        return False
    args = expr.get("args") or []
    if not args:
        return True  # COUNT(*)
    column = _column_name(args[0]) if len(args) == 1 else None
    return column is not None and column not in needs_calc


def _grouping(expr: dict) -> bool:
    """컬럼, 또는 컬럼을 날짜로 자르거나 시차만큼 옮긴 것"""
    if "col" in expr:
        return expr["col"] != "*"
    if "num" in expr or "str" in expr:
        return True
    if expr.get("fn") in _GROUPING_FUNCTIONS:
        for a in expr.get("args") or []:
            if not _grouping(a):
                return False
        return True
    return False


def _column_name(expr: dict) -> str | None:
    if "col" not in expr:
        return None
    return str(expr["col"]).split(".")[-1].lower()


def _conditions(conds: list) -> list:
    """AND · OR 을 펼친 조건 목록"""
    out = []
    for c in conds:
        if "and" in c:
            out += _conditions(c["and"])
        elif "or" in c:
            out += _conditions(c["or"])
        else:
            out.append(c)
    return out


def _one(parsed: dict) -> dict:
    """고친 응답은 지표 하나다. 목록으로 답했으면 첫 지표를 쓴다."""
    metrics = parsed.get("metrics")
    if isinstance(metrics, list) and metrics and isinstance(metrics[0], dict):
        return metrics[0]
    return parsed


def _fill(item: dict, p: dict) -> None:
    item["sql"] = str(p.get("sql") or "").strip().rstrip(";").strip()
    item["name"] = " ".join(str(p.get("name") or item["name"] or "").split())
    item["description"] = " ".join(str(p.get("description") or item["description"] or "").split())
    item["rationale"] = " ".join(str(p.get("rationale") or item["rationale"] or "").split())
    synonyms = p.get("synonyms")
    if isinstance(synonyms, str):
        synonyms = [synonyms]
    cleaned = []
    for s in synonyms or []:
        text = str(s or "").strip()
        if text and text not in cleaned and text != item["name"]:
            cleaned.append(text)
    item["synonyms"] = cleaned


def _guard(row: dict, item: dict, allow_full_scan: bool):
    """등록 확인이 대상 DB 에서 실행하기 직전에 부르는 검사 (지표 초안과 같은 기준)"""

    def guard(original: str, compiled: str) -> str | None:
        positional = positional_refs(original, row["driver"])
        if positional:
            return f"{', '.join(positional)} 에 순번(1, 2 …)을 썼습니다 — 순번 대신 컬럼이나 식을 그대로 적으십시오"
        item["full_scans"] = plan_full_scans(row, original, compiled)
        if item["full_scans"] and not allow_full_scan:
            return (
                f"실행 계획이 {', '.join(item['full_scans'])} 전체를 훑어 결과 비교를 하지 않았습니다 — "
                "인덱스가 있는 컬럼에 조건을 거십시오 (테스트 DB 면 '전체 훑기 허용' 으로 다시 돌리십시오)"
            )
        return None

    return guard


def plan_full_scans(row: dict, *sqls: str) -> list[str]:
    """SQL 들의 실행 계획(EXPLAIN 만)에서 전체를 훑는 큰 테이블. 못 보면 빈 목록이다."""
    found = []
    try:
        with read_only(row, PLAN_TIMEOUT_S) as cur:
            for sql in sqls:
                for name in full_scans(cur, Dialect(row["driver"]), sql):
                    if name not in found:
                        found.append(name)
    except Exception as error:
        log.warning("실행 계획을 보지 못했습니다: %s", _first_line(error))
    return found


def _indexes(row: dict, table: dict) -> list:
    names = set()
    for c in table["columns"]:
        names.add(c["name"])
    try:
        with read_only(row, PLAN_TIMEOUT_S) as cur:
            return read_indexes(cur, row, table, names)
    except Exception as error:
        log.warning("%s 인덱스를 읽지 못했습니다: %s", table["name"], _first_line(error))
        return []


def period_example(row: dict, table: dict, indexes: list) -> dict | None:
    """인덱스 앞 칸인 날짜 · 시각 컬럼의 가장 최근 값으로 예시 기간을 정한다 → {column, latest, start, end} 또는 None.

    최근 값은 그 인덱스를 거꾸로 한 행만 읽는다. 실행 계획이 전체를 훑으면 읽지 않고 다음 후보를 본다.
    기간은 저장된 값 기준의 날짜 [start, end) 다 — 최근 날을 끝으로 DRAFT_DAYS 일.
    """
    kinds = {}
    for c in table["columns"]:
        kinds[c["name"]] = temporal_kind(c.get("type", ""), row["driver"])
    heads = []
    for kind in ("datetime", "date"):  # 시각 컬럼이 날짜 컬럼보다 기록 시점에 가깝다
        for ix in indexes:
            head = ix["columns"][0]
            if kinds.get(head) == kind and head not in heads:
                heads.append(head)
    if not heads:
        return None
    d = Dialect(row["driver"])
    try:
        with read_only(row, PLAN_TIMEOUT_S) as cur:
            for column in heads:
                sql = (
                    f"SELECT {d.q(column)} FROM {d.table(table['name'], table.get('schema', ''))} WHERE {d.q(column)} IS NOT NULL "
                    f"ORDER BY {d.q(column)} DESC {d.limit(1)}"
                )
                if full_scans(cur, d, sql):
                    continue
                cur.execute(sql)
                found = cur.fetchone()
                if not found or found[0] is None:
                    continue
                latest = found[0]
                day = latest.date() if isinstance(latest, datetime.datetime) else latest
                if not isinstance(day, datetime.date):
                    continue
                start = day - datetime.timedelta(days=DRAFT_DAYS - 1)
                return {
                    "column": column,
                    "latest": str(latest),
                    "start": f"{start:%Y-%m-%d}",
                    "end": f"{day + datetime.timedelta(days=1):%Y-%m-%d}",
                }
    except Exception as error:
        log.warning("%s 의 최근 시각을 읽지 못했습니다: %s", table["name"], _first_line(error))
    return None


# ── 프롬프트 ───────────────────────────────────────────────────────────────


def auto_prompt(contract: dict, table: dict, indexes: list, count: int, period: dict | None = None) -> tuple:
    """→ (시스템 프롬프트, 사용자 메시지). period 는 period_example 의 예시 기간 (없으면 기간 조건을 넣지 않게 한다)"""
    driver = contract.get("driver") or "postgresql"
    domain = f"[업무 영역]\n{contract['domain']}\n" if contract.get("domain") else ""
    if period:
        period_rule = PERIOD_RULE.format(table=table["name"], column=period["column"])
    else:
        period_rule = NO_PERIOD_RULE
    system = AUTO_SYSTEM.format(
        name=contract.get("name", ""), driver=driver, domain=domain, count=count, period_rule=period_rule
    )

    note = table.get("purpose") or table.get("description") or ""
    lines = [f"[대상 테이블]\n[{table['name']}] {' '.join(note.split())}".rstrip()]
    for c in table["columns"]:
        lines.append(column_line(c, driver))

    lines.append("\n[인덱스]")
    for ix in indexes:
        lines.append(f"  - {ix['name']} ({', '.join(ix['columns'])})")
    if not indexes:
        lines.append("  (없음)")

    if period:
        c = period["column"]
        lines.append(f"\n[기간 예시] 가장 최근 값 {period['latest']} 기준 {DRAFT_DAYS}일")
        lines.append(f"  WHERE {table['name']}.{c} >= '{period['start']}' AND {table['name']}.{c} < '{period['end']}'")

    by_name = {}
    for t in contract["tables"]:
        by_name[t["name"]] = t
    related, pairs = [], []
    for r in contract.get("relations") or []:
        if r["left_table"] == table["name"]:
            other = r["right_table"]
        elif r["right_table"] == table["name"]:
            other = r["left_table"]
        else:
            continue
        pairs.append(f"  - {r['left_table']}.{r['left_column']} = {r['right_table']}.{r['right_column']}")
        if other != table["name"] and other not in related:
            related.append(other)
    lines.append("\n[관계]")
    lines += pairs or ["  (없음)"]
    shown = related[:MAX_RELATED]
    if shown:
        lines.append("\n[이어진 테이블]")
        for name in shown:
            t = by_name[name]
            cols = []
            for c in t["columns"][:MAX_RELATED_COLUMNS]:
                cols.append(f"{c['name']}({c.get('type', '')})")
            more = f" 외 {len(t['columns']) - MAX_RELATED_COLUMNS}개" if len(t["columns"]) > MAX_RELATED_COLUMNS else ""
            desc = " ".join(str(t.get("purpose") or t.get("description") or "").split())
            lines.append(f"- {name}{': ' + desc if desc else ''}\n  컬럼: {', '.join(cols)}{more}")
        if len(related) > len(shown):
            lines.append(f"(이어진 테이블 {len(related) - len(shown)}개 생략)")

    same, others = [], []
    for m in contract.get("metrics") or []:
        (same if table["name"] in (m.get("tables") or []) else others).append(m)
    lines.append("\n[이미 있는 지표]")
    for m in same:
        desc = " ".join(str(m.get("description") or "").split()) or "(설명 없음)"
        lines.append(f"- {m['name']}: {desc}\n  SQL: {' '.join(m['sql'].split())}")
    for m in others[:MAX_OTHER_METRICS]:
        lines.append(f"- {m['name']} ({', '.join(m.get('tables') or [])})")
    if not same and not others:
        lines.append("  (없음)")
    return system, "\n".join(lines)


def column_line(c: dict, driver: str) -> str:
    note = " ".join(str(c.get("description") or "").split())
    marks = []
    if c.get("is_pk"):
        marks.append("[PK]")
    if c.get("utc_offset") and temporal_kind(c.get("type", ""), driver) == "datetime":
        marks.append(f"[UTC {c['utc_offset']:+d}시간]")
    if c.get("needs_calc"):
        marks.append("[계산 필요]")
    tail = " ".join([note] + marks).strip()
    return f"  - {c['name']} ({c.get('type', '')}){': ' + tail if tail else ''}"


# ── 기록 ─────────────────────────────────────────────────────────────────


def _failed_item(table_name: str, stage: str | None, reason: str) -> dict:
    return {
        "table_name": table_name,
        "name": "",
        "description": "",
        "synonyms": [],
        "sql": "",
        "rationale": "",
        "status": "failed",
        "stage": stage,
        "reason": reason,
        "attempts": 0,
        "tries": [],
        "check": None,
        "full_scans": [],
        "metric_id": None,
    }


def _insert_item(run_id: UUID, item: dict) -> None:
    check = item["check"]
    if isinstance(check, MetricCheck):
        check = check.model_dump()
    _execute(
        """
        INSERT INTO metric_auto_item (run_id, table_name, name, description, synonyms, sql, rationale, status, stage,
                                      reason, attempts, tries, check_result, full_scans, metric_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """,
        str(run_id),
        item["table_name"],
        item["name"],
        item["description"],
        json.dumps(item["synonyms"], ensure_ascii=False),
        item["sql"],
        item["rationale"],
        item["status"],
        item["stage"],
        item["reason"],
        item["attempts"],
        json.dumps(item["tries"], ensure_ascii=False),
        json.dumps(check, ensure_ascii=False) if check is not None else None,
        json.dumps(item["full_scans"], ensure_ascii=False),
        item["metric_id"],
    )


def _progress(run_id: UUID, tables_done: int, prompt_tokens: int, completion_tokens: int) -> None:
    _execute(
        """
        UPDATE metric_auto_run r
           SET tables_done = %s, prompt_tokens = %s, completion_tokens = %s,
               proposed   = (SELECT count(*) FROM metric_auto_item i WHERE i.run_id = r.id AND i.attempts > 0),
               registered = (SELECT count(*) FROM metric_auto_item i WHERE i.run_id = r.id AND i.status = 'registered'),
               failed     = (SELECT count(*) FROM metric_auto_item i WHERE i.run_id = r.id AND i.status = 'failed'),
               skipped    = (SELECT count(*) FROM metric_auto_item i WHERE i.run_id = r.id AND i.status = 'skipped')
         WHERE r.id = %s
    """,
        tables_done,
        prompt_tokens,
        completion_tokens,
        str(run_id),
    )


def _close(run_id: UUID, status: str, error: str | None) -> None:
    _execute(
        "UPDATE metric_auto_run SET status = %s, error = %s, finished_at = now() WHERE id = %s",
        status,
        error,
        str(run_id),
    )


def _execute(query: str, *args) -> None:
    with db.connection() as conn:
        conn.execute(query, args)


def _row_to_run(r: dict) -> AutoRun:
    status = r["status"]
    if status == "running" and not background.is_active(r["id"]):
        status = "interrupted"  # 서버가 재시작돼 멈춘 실행
    return AutoRun(
        id=r["id"],
        system_id=r["system_id"],
        status=status,
        options=r["options"] or {},
        tables=r["tables"] or [],
        tables_done=r["tables_done"],
        proposed=r["proposed"],
        registered=r["registered"],
        failed=r["failed"],
        skipped=r["skipped"],
        prompt_tokens=r["prompt_tokens"],
        completion_tokens=r["completion_tokens"],
        error=r["error"],
        started_at=r["started_at"].isoformat(),
        finished_at=r["finished_at"].isoformat() if r["finished_at"] else None,
    )


def _row_to_item(r: dict) -> AutoItem:
    tries = []
    for t in r["tries"] or []:
        tries.append(AutoTry(**t))
    return AutoItem(
        id=r["id"],
        table_name=r["table_name"],
        name=r["name"],
        description=r["description"],
        synonyms=r["synonyms"] or [],
        sql=r["sql"],
        rationale=r["rationale"],
        status=r["status"],
        stage=r["stage"],
        reason=r["reason"],
        attempts=r["attempts"],
        tries=tries,
        check=MetricCheck(**r["check_result"]) if r["check_result"] else None,
        full_scans=r["full_scans"] or [],
        metric_id=r["metric_id"],
        created_at=r["created_at"].isoformat(),
    )


def _first_line(error) -> str:
    lines = str(getattr(error, "detail", None) or error).strip().splitlines()
    return lines[0] if lines else type(error).__name__
