"""등록된 지표 검증: 지표마다 여러 점검을 돌려 통과 · 주의 · 실패를 매기고, 점검마다 이유와 근거를 남긴다.

점검 (앞 단계가 실패하면 뒤 단계는 건너뛴다)
  compile    저장된 AST 가 지금 스키마로 컴파일되는가
  roundtrip  원본 SQL 을 지금 다시 옮겨도 저장된 AST 와 같은가 (등록 뒤 UTC 보정 · 컬럼이 바뀌었는지)
  duplicate  다른 지표와 컴파일 결과가 같은가
  plan       실행 계획이 큰 테이블 전체를 훑는가 (EXPLAIN 만)
  verify     원본 SQL 과 AST 로 만든 SQL 의 결과가 대상 DB 에서 같은가 (등록 확인과 같은 비교)
  result     결과가 비었거나 값이 모두 NULL 인 컬럼이 있는가 (표본 몇 행을 남긴다)
  llm        LLM 이 SQL 이 이름 · 설명과 맞는지, JOIN 으로 부풀지 않는지 등을 검토한다
"""

import datetime
import decimal
import json
import logging
import time
from uuid import UUID, uuid4

import background
import db
from compiler import Compiler, Dialect, ast_tables
from contract import load_contract, load_metric_rows
from metric_auto import column_line, plan_full_scans
from metrics import read_only, verify
from models import ApiException, ValidationCheck, ValidationItem, ValidationRequest, ValidationRun, ValidationRunDetail
from selector import Step, ask_json, model_name
from sql_ast import parse_sql
from systems import connection_row

log = logging.getLogger("nl2sql.validate")

SAMPLE_ROWS = 20
RESULT_TIMEOUT_S = 60
LABELS = {
    "compile": "컴파일",
    "roundtrip": "원본 재변환",
    "duplicate": "중복",
    "plan": "실행 계획",
    "verify": "결과 비교",
    "result": "결과 점검",
    "llm": "의미 검토",
}

REVIEW_SYSTEM = """당신은 '{name}' 데이터베이스({driver})에 등록된 업무 지표를 검토하는 역할입니다.
지표는 이름 · 설명 · 같은 말과 SQL 로 되어 있고, 질문마다 다른 LLM 이 설명을 읽고 지표를 고른 뒤 SQL 의 조건만 바꿔 씁니다.
{domain}
[점검할 것]
- SQL 이 이름과 설명이 말하는 계산을 하는가 (쓰는 컬럼, 집계 방식, 묶음 단위, 빼는 행).
- JOIN 으로 행이 불어나 합계가 부풀지 않는가 (팩트 테이블끼리 JOIN, 1:N 의 N 쪽을 JOIN 한 뒤 SUM).
- 나눗셈에 0 나누기나 정수 나눗셈이 없는가.
- [계산 필요] 컬럼을 그대로 SUM · AVG 하지 않는가.
- NULL 때문에 결과가 틀리지 않는가 (COUNT(컬럼) 과 COUNT(*) 의 차이, NULL 이 섞인 합과 비율).
- 설명만 읽고 이 지표를 고를 수 있는가 (설명이 비었거나 계산과 다르면 문제).
- [결과 표본] 의 값이 상식에 맞는가 (음수가 나올 수 없는 값의 음수, 0 ~ 1 을 벗어난 비율 등).
[규칙]
- 주어진 정보로 확인되는 문제만 적습니다. 추측은 적지 않습니다.
- severity 는 fail(결과가 틀림) 또는 warn(틀릴 수 있거나 설명이 부족함) 입니다.
- verdict 는 문제가 없으면 pass, warn 만 있으면 warn, fail 이 하나라도 있으면 fail 입니다.
- summary 에는 판단을 한두 문장으로, issue 의 message 에는 무엇이 왜 문제인지와 고칠 방법을 적습니다.
- 설명 없이 JSON 객체 하나로만 답합니다:
  {{"verdict": "pass", "summary": "판단 요약", "issues": [{{"severity": "warn", "message": "문제와 고칠 방법"}}]}}"""


# ── 시작 · 조회 ───────────────────────────────────────────────────────────


def start_validation(system_id: UUID, req: ValidationRequest, model) -> ValidationRun:
    """고른 지표만 검증한다. 고르지 않으면 돌리지 않는다 — 전체도 사람이 모두 골라서 돌린다."""
    if not req.metric_ids:
        raise ApiException(400, "검증할 지표를 고르십시오")
    rows = _pick(system_id, req.metric_ids)
    ids = []
    for r in rows:
        ids.append(str(r["id"]))
    run_id = uuid4()
    options = {
        "run_sql": req.run_sql,
        "use_llm": req.use_llm,
        "allow_full_scan": req.allow_full_scan,
        "metric_ids": ids,
    }
    _execute(
        "INSERT INTO metric_validation_run (id, system_id, options, total) VALUES (%s, %s, %s, %s)",
        str(run_id),
        str(system_id),
        json.dumps(options, ensure_ascii=False),
        len(ids),
    )

    def work():
        run_validation(system_id, run_id, ids, req, model)

    def on_error(error):
        _close(run_id, "error", _first_line(error))

    if not background.start(f"validate:{system_id}", run_id, work, on_error):
        _execute("DELETE FROM metric_validation_run WHERE id = %s", str(run_id))
        raise ApiException(409, "이 시스템에서 지표 검증이 이미 돌고 있습니다")
    log.info("지표 검증 시작: run=%s metrics=%d", run_id, len(ids))
    return get_validation(system_id, run_id)


def list_validations(system_id: UUID, limit: int = 50) -> list[ValidationRun]:
    runs = []
    for r in db.query(
        f"SELECT r.*, {_DONE} FROM metric_validation_run r WHERE r.system_id = %s ORDER BY r.started_at DESC LIMIT %s",
        str(system_id),
        limit,
    ):
        runs.append(_row_to_run(r))
    return runs


def get_validation(system_id: UUID, run_id: UUID) -> ValidationRunDetail:
    row = db.one(
        f"SELECT r.*, {_DONE} FROM metric_validation_run r WHERE r.id = %s AND r.system_id = %s",
        str(run_id),
        str(system_id),
    )
    if row is None:
        raise ApiException(404, "지표 검증 실행을 찾을 수 없습니다")
    items = []
    for r in db.query("SELECT * FROM metric_validation_item WHERE run_id = %s ORDER BY id", str(run_id)):
        items.append(_row_to_item(r))
    return ValidationRunDetail(**_row_to_run(row).model_dump(), items=items)


def metric_validations(system_id: UUID, metric_id: UUID, limit: int = 20) -> list[ValidationItem]:
    """지표 하나의 검증 이력 (최근 것이 먼저)"""
    items = []
    for r in db.query(
        """
        SELECT i.* FROM metric_validation_item i
          JOIN metric_validation_run r ON r.id = i.run_id
         WHERE r.system_id = %s AND i.metric_id = %s
         ORDER BY i.created_at DESC LIMIT %s
    """,
        str(system_id),
        str(metric_id),
        limit,
    ):
        items.append(_row_to_item(r))
    return items


def _pick(system_id: UUID, metric_ids: list[UUID]) -> list[dict]:
    rows = load_metric_rows(system_id)
    wanted = set()
    for i in metric_ids:
        wanted.add(str(i))
    picked = []
    for r in rows:
        if str(r["id"]) in wanted:
            picked.append(r)
    if len(picked) != len(wanted):
        raise ApiException(404, "이 시스템에 없는 지표가 있습니다")
    return picked


# ── 실행 ──────────────────────────────────────────────────────────────────


def run_validation(system_id: UUID, run_id: UUID, ids: list[str], req: ValidationRequest, model) -> None:
    contract = load_contract(system_id)
    row = connection_row(system_id) if req.run_sql else None
    compiler = Compiler(contract)
    # 중복 점검은 고른 지표가 아니라 시스템의 모든 지표와 견준다
    everything = load_metric_rows(system_id)
    compiled_all = {}
    for m in everything:
        try:
            compiled_all[str(m["id"])] = {"name": m["name"], "sql": compiler.compile(m["ast"])}
        except ValueError:
            pass
    wanted = set(ids)
    prompt_tokens = completion_tokens = 0
    for m in everything:
        if str(m["id"]) not in wanted:
            continue
        started = time.monotonic()
        step = Step("지표 검증", model=model_name(model) if model is not None else "")
        try:
            checks = validate_metric(
                system_id, m, contract, compiler, compiled_all, row, model if req.use_llm else None, step,
                req.allow_full_scan,
            )
        except Exception as error:
            log.exception("지표 검증 실패: %s", m["name"])
            checks = [_check("compile", "fail", f"검증 중 오류가 났습니다: {_first_line(error)}")]
        prompt_tokens += step.prompt_tokens
        completion_tokens += step.completion_tokens
        verdict, summary = overall(checks)
        _execute(
            """
            INSERT INTO metric_validation_item (run_id, metric_id, metric_name, metric_version, verdict, summary, checks, elapsed_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
            str(run_id),
            str(m["id"]),
            m["name"],
            m["version"],
            verdict,
            summary,
            json.dumps(checks, ensure_ascii=False, default=str),
            int((time.monotonic() - started) * 1000),
        )
        _progress(run_id, prompt_tokens, completion_tokens)
    _close(run_id, "done", None)
    log.info("지표 검증 끝: run=%s", run_id)


def validate_metric(
    system_id, m: dict, contract: dict, compiler: Compiler, compiled_all: dict, row, model, step, allow_full_scan=False
) -> list[dict]:
    """지표 하나의 점검 목록. row 가 없으면 대상 DB 점검을, model 이 없으면 의미 검토를 건너뛴다.
    실행 계획이 큰 테이블 전체를 훑으면 allow_full_scan 일 때만 결과 비교 · 결과 점검을 돌린다."""
    checks = []

    # 1) 컴파일
    try:
        compiled = compiler.compile(m["ast"])
    except ValueError as error:
        compiled = None
        checks.append(_check("compile", "fail", f"지금 스키마로 컴파일되지 않습니다 — {error}"))
    else:
        if m["status"] == "broken":
            checks.append(
                _check("compile", "warn", "컴파일은 되지만 상태가 '깨짐' 으로 남아 있습니다 — 동기화하면 다시 매겨집니다",
                       {"sql": compiled, "broken_reason": m["broken_reason"]})
            )
        else:
            checks.append(_check("compile", "pass", "지금 스키마로 컴파일됩니다", {"sql": compiled}))

    # 2) 원본 재변환
    try:
        parsed = parse_sql(m["source_sql"], contract)
    except ValueError as error:
        checks.append(_check("roundtrip", "warn", f"원본 SQL 을 지금 스키마로 다시 옮기지 못합니다 — {error}"))
    else:
        if parsed["ast"] == m["ast"]:
            checks.append(_check("roundtrip", "pass", "원본 SQL 을 다시 옮겨도 저장된 AST 와 같습니다", {"warnings": parsed["warnings"]}))
        else:
            checks.append(
                _check(
                    "roundtrip", "warn",
                    "원본 SQL 을 지금 다시 옮기면 저장된 AST 와 다릅니다 — 등록 뒤 UTC 보정이나 컬럼이 바뀌었을 수 있습니다. "
                    "지표를 다시 저장하면 맞춰집니다",
                    {"stored_ast": m["ast"], "current_ast": parsed["ast"], "warnings": parsed["warnings"]},
                )
            )

    # 3) 중복
    if compiled is None:
        checks.append(_skip("duplicate", "컴파일되지 않아 건너뛰었습니다"))
    else:
        same = []
        for other_id, other in compiled_all.items():
            if other_id != str(m["id"]) and other["sql"] == compiled:
                same.append(other["name"])
        if same:
            checks.append(_check("duplicate", "warn", f"컴파일 결과가 같은 지표가 있습니다: {', '.join(same)}", {"metrics": same}))
        else:
            checks.append(_check("duplicate", "pass", "같은 계산의 다른 지표가 없습니다"))

    # 4) ~ 6) 대상 DB
    sample = None
    if row is None:
        for key in ("plan", "verify", "result"):
            checks.append(_skip(key, "대상 DB 점검을 끄고 돌렸습니다"))
    elif compiled is None:
        for key in ("plan", "verify", "result"):
            checks.append(_skip(key, "컴파일되지 않아 건너뛰었습니다"))
    else:
        plan = _plan_check(row, m["source_sql"], compiled)
        checks.append(plan)
        if plan["detail"].get("full_scans") and not allow_full_scan:
            for key in ("verify", "result"):
                checks.append(_skip(key, "실행 계획이 큰 테이블 전체를 훑어 실행하지 않았습니다 (테스트 DB 면 '전체 훑기 허용' 으로 다시 돌리십시오)"))
        else:
            checks.append(_verify_check(system_id, m["source_sql"], compiled))
            result = _result_check(row, compiled)
            checks.append(result)
            if result["detail"].get("rows"):
                sample = result["detail"]

    # 7) 의미 검토
    if model is None:
        checks.append(_skip("llm", "의미 검토를 끄고 돌렸습니다"))
    else:
        checks.append(_review(model, contract, m, sample, step))
    return checks


def _plan_check(row: dict, original: str, compiled: str) -> dict:
    if row["driver"] == "oracle":
        return _skip("plan", "Oracle 은 읽기 전용 트랜잭션에서 실행 계획을 볼 수 없어 건너뛰었습니다")
    scanned = plan_full_scans(row, original, compiled)
    if scanned:
        return _check(
            "plan", "warn",
            f"실행 계획이 {', '.join(scanned)} 전체를 훑습니다 — 질문마다 기간 조건이 붙지 않으면 운영 DB 에 부담이 됩니다",
            {"full_scans": scanned},
        )
    return _check("plan", "pass", "큰 테이블 전체를 훑지 않습니다")


def _verify_check(system_id: UUID, original: str, compiled: str) -> dict:
    try:
        rows = verify(system_id, original, compiled)
    except ValueError as error:
        return _check("verify", "fail", str(error))
    except ApiException as error:
        return _check("verify", "fail", str(error.detail))
    return _check("verify", "pass", f"원본 SQL 과 AST 로 만든 SQL 의 결과가 같습니다 ({rows:,}행)", {"rows": rows})


def _result_check(row: dict, compiled: str) -> dict:
    """결과 표본 몇 행과 컬럼마다 NULL 수. 결과가 비었거나 값이 모두 NULL 인 컬럼이 있으면 주의."""
    d = Dialect(row["driver"])
    try:
        with read_only(row, RESULT_TIMEOUT_S) as cur:
            cur.execute(f"SELECT * FROM ({compiled}) t {d.limit(SAMPLE_ROWS)}")
            columns = []
            for desc in cur.description:
                columns.append(str(desc[0]))
            fetched = cur.fetchall()
            counts = []
            for c in columns:
                counts.append(f"COUNT({d.q(c)})")
            cur.execute(f"SELECT COUNT(*), {', '.join(counts)} FROM ({compiled}) t")
            stats = cur.fetchone()
    except ApiException as error:
        return _check("result", "fail", str(error.detail))
    except Exception as error:
        return _check("result", "fail", f"결과를 읽지 못했습니다: {_first_line(error)}")
    total = int(stats[0])
    nulls = {}
    all_null = []
    for i, c in enumerate(columns):
        nulls[c] = total - int(stats[i + 1])
        if total and nulls[c] == total:
            all_null.append(c)
    rows = []
    for r in fetched:
        rows.append(_shown(r))
    detail = {"total": total, "columns": columns, "rows": rows, "nulls": nulls}
    if total == 0:
        return _check("result", "warn", "결과가 0행입니다 — 맞는 데이터가 없거나 JOIN · 조건이 행을 모두 걸러 냅니다", detail)
    if all_null:
        return _check("result", "warn", f"{', '.join(all_null)} 값이 모두 NULL 입니다 ({total:,}행)", detail)
    return _check("result", "pass", f"{total:,}행, 값이 모두 NULL 인 컬럼이 없습니다", detail)


def _review(model, contract: dict, m: dict, sample: dict | None, step: Step) -> dict:
    driver = contract.get("driver") or "postgresql"
    domain = f"[업무 영역]\n{contract['domain']}\n" if contract.get("domain") else ""
    system = REVIEW_SYSTEM.format(name=contract.get("name", ""), driver=driver, domain=domain)
    user = review_prompt(contract, m, sample)

    def validate(p):
        verdict = str(p.get("verdict") or "").strip().lower()
        if verdict not in ("pass", "warn", "fail"):
            raise ValueError('"verdict" 는 pass · warn · fail 중 하나입니다')
        issues = []
        for i in p.get("issues") or []:
            if not isinstance(i, dict) or not str(i.get("message") or "").strip():
                continue
            severity = "fail" if str(i.get("severity") or "").lower() == "fail" else "warn"
            issues.append({"severity": severity, "message": " ".join(str(i["message"]).split())})
        return {"verdict": verdict, "summary": " ".join(str(p.get("summary") or "").split()), "issues": issues}

    try:
        parsed = ask_json(model, system, user, step, validate)
    except ValueError as error:
        return _check("llm", "skip", str(error))
    except Exception as error:
        return _check("llm", "skip", f"LLM 을 부르지 못했습니다: {_first_line(error)}")
    message = parsed["summary"] or ("문제를 찾지 못했습니다" if parsed["verdict"] == "pass" else "문제가 있습니다")
    return _check(
        "llm", parsed["verdict"], message,
        {"issues": parsed["issues"], "prompt_tokens": step.prompt_tokens, "completion_tokens": step.completion_tokens},
    )


def review_prompt(contract: dict, m: dict, sample: dict | None) -> str:
    driver = contract.get("driver") or "postgresql"
    lines = [f"[지표]\n이름: {m['name']}", f"설명: {' '.join(str(m['description'] or '').split()) or '(없음)'}"]
    if m.get("synonyms"):
        lines.append(f"같은 말: {', '.join(m['synonyms'])}")
    lines.append(f"SQL:\n{m['source_sql']}")

    used = ast_tables(m["ast"])
    lines.append("\n[쓰는 테이블]")
    for t in contract["tables"]:
        if t["name"] in used:
            note = " ".join(str(t.get("purpose") or t.get("description") or "").split())
            lines.append(f"[{t['name']}] {note}".rstrip())
            for c in t["columns"]:
                lines.append(column_line(c, driver))
    pairs = []
    for r in contract.get("relations") or []:
        if r["left_table"] in used and r["right_table"] in used:
            pairs.append(f"  - {r['left_table']}.{r['left_column']} = {r['right_table']}.{r['right_column']}")
    if pairs:
        lines.append("\n[관계]")
        lines += pairs

    if sample:
        lines.append(f"\n[결과 표본] 전체 {sample['total']:,}행 중 {len(sample['rows'])}행")
        lines.append(" | ".join(sample["columns"]))
        for r in sample["rows"]:
            lines.append(" | ".join(r))
    return "\n".join(lines)


def overall(checks: list[dict]) -> tuple[str, str]:
    """→ (판정, 요약). 실패가 하나라도 있으면 fail, 주의만 있으면 warn."""
    verdict = "pass"
    notes = []
    for c in checks:
        if c["status"] == "fail":
            verdict = "fail"
        elif c["status"] == "warn" and verdict == "pass":
            verdict = "warn"
    for status in ("fail", "warn"):
        for c in checks:
            if c["status"] == status and len(notes) < 3:
                notes.append(f"{c['label']}: {c['message']}")
    return verdict, " / ".join(notes) if notes else "모든 점검을 통과했습니다"


# ── 기록 ─────────────────────────────────────────────────────────────────

_DONE = "(SELECT count(*) FROM metric_validation_item i WHERE i.run_id = r.id) AS done"


def _check(key: str, status: str, message: str, detail: dict | None = None) -> dict:
    return {"key": key, "label": LABELS[key], "status": status, "message": message, "detail": detail or {}}


def _skip(key: str, message: str) -> dict:
    return _check(key, "skip", message)


def _progress(run_id: UUID, prompt_tokens: int, completion_tokens: int) -> None:
    _execute(
        """
        UPDATE metric_validation_run r
           SET prompt_tokens = %s, completion_tokens = %s,
               passed = (SELECT count(*) FROM metric_validation_item i WHERE i.run_id = r.id AND i.verdict = 'pass'),
               warned = (SELECT count(*) FROM metric_validation_item i WHERE i.run_id = r.id AND i.verdict = 'warn'),
               failed = (SELECT count(*) FROM metric_validation_item i WHERE i.run_id = r.id AND i.verdict = 'fail')
         WHERE r.id = %s
    """,
        prompt_tokens,
        completion_tokens,
        str(run_id),
    )


def _close(run_id: UUID, status: str, error: str | None) -> None:
    _execute(
        "UPDATE metric_validation_run SET status = %s, error = %s, finished_at = now() WHERE id = %s",
        status,
        error,
        str(run_id),
    )


def _execute(query: str, *args) -> None:
    with db.connection() as conn:
        conn.execute(query, args)


def _row_to_run(r: dict) -> ValidationRun:
    status = r["status"]
    if status == "running" and not background.is_active(r["id"]):
        status = "interrupted"  # 서버가 재시작돼 멈춘 실행
    return ValidationRun(
        id=r["id"],
        system_id=r["system_id"],
        status=status,
        options=r["options"] or {},
        total=r["total"],
        done=r["done"],
        passed=r["passed"],
        warned=r["warned"],
        failed=r["failed"],
        prompt_tokens=r["prompt_tokens"],
        completion_tokens=r["completion_tokens"],
        error=r["error"],
        started_at=r["started_at"].isoformat(),
        finished_at=r["finished_at"].isoformat() if r["finished_at"] else None,
    )


def _row_to_item(r: dict) -> ValidationItem:
    checks = []
    for c in r["checks"] or []:
        checks.append(ValidationCheck(**c))
    return ValidationItem(
        id=r["id"],
        run_id=r["run_id"],
        metric_id=r["metric_id"],
        metric_name=r["metric_name"],
        metric_version=r["metric_version"],
        verdict=r["verdict"],
        summary=r["summary"],
        checks=checks,
        elapsed_ms=r["elapsed_ms"],
        created_at=r["created_at"].isoformat(),
    )


def _shown(values) -> list[str]:
    out = []
    for v in values:
        if v is None:
            out.append("NULL")
        elif isinstance(v, datetime.datetime):
            out.append(v.isoformat(sep=" ", timespec="seconds"))
        elif isinstance(v, decimal.Decimal):
            out.append(format(v.normalize(), "f") if v == v.to_integral() else str(v))
        else:
            text = str(v)
            out.append(text if len(text) <= 60 else text[:57] + "...")
    return out


def _first_line(error) -> str:
    lines = str(getattr(error, "detail", None) or error).strip().splitlines()
    return lines[0] if lines else type(error).__name__
