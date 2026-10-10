"""평가셋 실행기.

  # 모델 없이: 평가셋의 AST 가 기대 SQL 로 컴파일되는지 (평가셋·컴파일러 점검)
  python backend/evaluate.py --cases eval/shoppingmall_cases.json --contract eval/shoppingmall_contract.json --compile-only

  # 모델로: 질문마다 에이전트를 돌려 기대 SQL 과 비교하고 리포트를 남긴다 (LLM_* 환경변수 필요)
  python backend/evaluate.py --cases eval/shoppingmall_cases.json --contract eval/shoppingmall_contract.json

  # 메타데이터 DB 의 시스템으로 (DB_URL 필요)
  python backend/evaluate.py --cases <파일> --system-id <uuid>                     # TO-BE (LLM 선택)
  python backend/evaluate.py --cases <파일> --system-id <uuid> --selector tfidf    # 비교 기준 (한 번에 검색)
"""
import argparse
import datetime
import json
import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

from compiler import Compiler  # noqa: E402

REPORT_DIR = BASE_DIR.parent / "eval" / "reports"


def normalize_sql(sql: str | None) -> str:
    """공백 차이만 무시한다. 문자열 리터럴 안은 건드리지 않는다."""
    if sql is None:
        return ""
    parts = re.split(r"('(?:[^']|'')*')", sql.strip())
    return "".join(p if i % 2 else re.sub(r"\s+", " ", p) for i, p in enumerate(parts)).strip()


def _split_top(text: str) -> list[str]:
    """괄호·문자열 밖의 쉼표로 나눈다."""
    parts, depth, quote, start = [], 0, False, 0
    for i, ch in enumerate(text):
        if ch == "'":
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
        elif not quote and depth == 0 and ch == ",":
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts


_ALIAS = re.compile(r'^(.*)\s+AS\s+([`"][^`"]+[`"])$', re.S)


def loose_sql(sql: str | None) -> str:
    """별칭 이름 차이를 지운 SQL. SELECT 의 'expr AS "별칭"' 은 expr 로, 다른 절의 별칭 참조도 expr 로 바꾼다.

    모델이 고른 별칭("member_count")과 평가셋의 별칭("count_*")만 다른 답을 맞은 것으로 센다.
    """
    text = normalize_sql(sql)
    m = re.match(r"^(SELECT(?: DISTINCT)? )(.*?)( FROM .*)$", text, re.S)
    if not m:
        return text
    head, select, rest = m.groups()
    items, aliases = [], {}
    for item in _split_top(select):
        a = _ALIAS.match(item)
        if a:
            aliases[a.group(2)] = a.group(1).strip()
            items.append(a.group(1).strip())
        else:
            items.append(item)
    for alias, expr in aliases.items():
        rest = rest.replace(alias, expr)
    return head + ", ".join(items) + rest


def load_cases(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"cases": data}
    for i, case in enumerate(data["cases"]):
        if not case.get("question") or "expected_sql" not in case:
            raise SystemExit(f"평가셋 {i + 1}번 항목에 question 과 expected_sql 이 필요합니다")
    return data


def run_compile_only(contract: dict, data: dict) -> dict:
    compiler = Compiler(contract)
    results = []
    for case in data["cases"]:
        if not case.get("ast"):
            results.append({"question": case["question"], "status": "skipped", "reason": "ast 없음"})
            continue
        try:
            sql = compiler.compile(case["ast"])
            error = None
        except (ValueError, KeyError, TypeError) as e:
            sql, error = None, str(e)
        ok = sql is not None and normalize_sql(sql) == normalize_sql(case["expected_sql"])
        results.append({"question": case["question"], "status": "pass" if ok else "fail",
                        "sql": sql, "expected_sql": case["expected_sql"], "error": error})
    return _summary(results, mode="compile-only")


def judge(outcome: dict, case: dict) -> dict:
    """한 문항의 단계별 채점. 정답이 주어진 단계만 채점한다 (없으면 None).

    table_hit: 정답 테이블이 모두 골라졌나 (재현율) · metric_hit: 정답 지표를 골랐나
    ('맞는 지표 없음' 이 정답이면 아무것도 고르지 않아야 맞다) · sql_hit: 기대 SQL 과 같나 (별칭 무시)
    """
    picked_tables = {t.lower() for t in outcome.get("tables", [])}
    expected_tables = case.get("expected_tables")
    table_hit = None if not expected_tables else all(t.lower() in picked_tables for t in expected_tables)
    metric_hit = None
    if "expected_metric" in case:
        picked = [m.lower() for m in outcome.get("metrics", [])]
        expected = case["expected_metric"]
        metric_hit = (not picked) if expected is None else (expected.lower() in picked)
    sql_hit = None
    if case.get("expected_sql"):
        sql_hit = bool(outcome.get("sql")) and loose_sql(outcome["sql"]) == loose_sql(case["expected_sql"])
    return {"table_hit": table_hit, "metric_hit": metric_hit, "sql_hit": sql_hit}


def run_model(contract: dict, data: dict, today: datetime.date | None, selector: str = "llm", model=None) -> dict:
    from ask import run_question
    from llm import make_model

    model = model or make_model()
    results = []
    for i, case in enumerate(data["cases"], start=1):
        started = time.monotonic()
        try:
            outcome = run_question(contract, case["question"], model, today=today, selector=selector)
        except Exception as e:  # 한 문항의 장애가 평가 전체를 멈추지 않게
            outcome = {"sql": None, "error": f"{type(e).__name__}: {e}", "attempts": 0, "ast": None,
                       "clarification": None, "tables": [], "metrics": [], "total_tokens": 0}
        elapsed = (time.monotonic() - started) * 1000
        expected = case.get("expected_sql")
        if expected and outcome["sql"] and normalize_sql(outcome["sql"]) == normalize_sql(expected):
            status = "pass"
        elif expected and outcome["sql"] and loose_sql(outcome["sql"]) == loose_sql(expected):
            status = "pass_alias"
        elif outcome["sql"]:
            status = "wrong_sql" if expected else "sql"
        elif outcome["clarification"]:
            status = "clarified"
        else:
            status = "fail"
        results.append({
            "question": case["question"], "status": status, "attempts": outcome["attempts"],
            "elapsed_ms": round(elapsed), "sql": outcome["sql"], "expected_sql": expected,
            "tables": outcome.get("tables", []), "metrics": outcome.get("metrics", []),
            "total_tokens": outcome.get("total_tokens", 0), **judge(outcome, case),
            "ast": outcome["ast"], "error": outcome["error"], "clarification": outcome["clarification"],
        })
        hits = " ".join(f"{k[:-4]}={'O' if v else 'X'}" for k, v in judge(outcome, case).items() if v is not None)
        print(f"[{i}/{len(data['cases'])}] {status:9s} {hits:24s} {case['question']}", flush=True)
    return _summary(results, mode=f"model-{selector}")


def _summary(results: list[dict], mode: str) -> dict:
    counted = [r for r in results if r["status"] != "skipped"]
    by_status: dict = {}
    for r in counted:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    attempts = [r["attempts"] for r in counted if "attempts" in r]

    def rate(key):
        judged = [r[key] for r in counted if r.get(key) is not None]
        return round(sum(judged) / len(judged), 4) if judged else None

    with_sql = [r for r in counted if r.get("expected_sql")]
    tokens = [r["total_tokens"] for r in counted if "total_tokens" in r]
    elapsed = [r["elapsed_ms"] for r in counted if "elapsed_ms" in r]
    return {
        "mode": mode,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "total": len(counted),
        "accuracy": round(by_status.get("pass", 0) / len(with_sql), 4) if with_sql else None,
        # 별칭 이름만 다른 답까지 맞은 것으로 센 정확도
        "accuracy_alias_insensitive": round(
            (by_status.get("pass", 0) + by_status.get("pass_alias", 0)) / len(with_sql), 4) if with_sql else None,
        # 단계별 — 어느 단계에서 틀렸는지
        "table_recall": rate("table_hit"),
        "metric_accuracy": rate("metric_hit"),
        "sql_accuracy": rate("sql_hit"),
        "avg_tokens": round(sum(tokens) / len(tokens)) if tokens else None,
        "avg_elapsed_ms": round(sum(elapsed) / len(elapsed)) if elapsed else None,
        "by_status": by_status,
        "avg_attempts": round(sum(attempts) / len(attempts), 2) if attempts else None,
        "failures": [r for r in counted if r["status"] not in ("pass", "pass_alias")
                     or r.get("table_hit") is False or r.get("metric_hit") is False],
        "results": results,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="nl2sql 평가셋 실행기")
    parser.add_argument("--cases", type=Path, required=True, help="평가셋 JSON 파일")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--contract", type=Path, help="계약서 JSON 파일")
    source.add_argument("--system-id", help="메타데이터 DB 의 시스템 id")
    parser.add_argument("--selector", choices=("llm", "tfidf"), default="llm",
                        help="llm = 테이블 추론 → 지표 선택 (TO-BE) · tfidf = 한 번에 검색 (비교 기준)")
    parser.add_argument("--compile-only", action="store_true", help="모델 없이 ast → expected_sql 만 확인")
    parser.add_argument("--out", type=Path, help="리포트 경로 (기본: eval/reports/<시각>.json)")
    args = parser.parse_args(argv)

    data = load_cases(args.cases)
    today = datetime.date.fromisoformat(data["today"]) if data.get("today") else None
    if args.contract:
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
    else:
        import db
        from contract import load_contract
        from uuid import UUID
        db.init_pool()
        try:
            contract = load_contract(UUID(args.system_id), active_only=not args.compile_only)
        finally:
            db.close_pool()
    report = run_compile_only(contract, data) if args.compile_only else run_model(contract, data, today, args.selector)

    out = args.out or REPORT_DIR / f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{report['mode']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n테이블 재현율 {report.get('table_recall')} · 지표 정확도 {report.get('metric_accuracy')} · "
          f"SQL 일치 {report.get('sql_accuracy')} (정확히 {report['accuracy']})")
    print(f"{report['by_status']} · 평균 시도 {report['avg_attempts']} · 평균 토큰 {report.get('avg_tokens')} · 리포트 {out}")
    for f in report["failures"][:20]:
        print(f"  - [{f['status']}] {f['question']}: {f.get('error') or f.get('clarification') or f.get('sql')}")
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
