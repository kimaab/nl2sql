"""평가셋 실행기.

  # 모델 없이: 평가셋의 AST 가 기대 SQL 로 컴파일되는지 (평가셋·컴파일러 점검)
  python backend/evaluate.py --cases eval/shoppingmall_cases.json --contract eval/shoppingmall_contract.json --compile-only

  # 모델로: 질문마다 에이전트를 돌려 기대 SQL 과 비교하고 리포트를 남긴다 (LLM_* 환경변수 필요)
  python backend/evaluate.py --cases eval/shoppingmall_cases.json --contract eval/shoppingmall_contract.json

  # 계약서를 메타데이터 DB 에서 (DB_URL 필요). 질문 기록에서 내보낸 평가셋도 같은 형식이다.
  python backend/evaluate.py --cases exported.json --datasource-id <uuid>
"""
import argparse
import datetime
import json
import os
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


def run_model(contract: dict, data: dict, today: datetime.date | None) -> dict:
    from langchain_openai import ChatOpenAI
    from ask import run_question

    model = ChatOpenAI(
        base_url=os.environ.get("LLM_BASE_URL"),
        api_key=os.environ.get("LLM_API_KEY", "not-needed"),
        model=os.environ.get("LLM_MODEL", "gpt-3.5-turbo"),
        temperature=0.0,
    )
    results = []
    for i, case in enumerate(data["cases"], start=1):
        started = time.monotonic()
        try:
            outcome = run_question(contract, case["question"], model, today=today)
        except Exception as e:  # 한 문항의 장애가 평가 전체를 멈추지 않게
            outcome = {"sql": None, "error": f"{type(e).__name__}: {e}", "attempts": 0, "ast": None,
                       "clarification": None}
        elapsed = (time.monotonic() - started) * 1000
        if outcome["sql"] and normalize_sql(outcome["sql"]) == normalize_sql(case["expected_sql"]):
            status = "pass"
        elif outcome["sql"] and loose_sql(outcome["sql"]) == loose_sql(case["expected_sql"]):
            status = "pass_alias"
        elif outcome["sql"]:
            status = "wrong_sql"
        elif outcome["clarification"]:
            status = "clarified"
        else:
            status = "fail"
        results.append({
            "question": case["question"], "status": status, "attempts": outcome["attempts"],
            "elapsed_ms": round(elapsed), "sql": outcome["sql"], "expected_sql": case["expected_sql"],
            "ast": outcome["ast"], "error": outcome["error"], "clarification": outcome["clarification"],
        })
        print(f"[{i}/{len(data['cases'])}] {status:9s} {case['question']}", flush=True)
    return _summary(results, mode="model")


def _summary(results: list[dict], mode: str) -> dict:
    counted = [r for r in results if r["status"] != "skipped"]
    by_status: dict = {}
    for r in counted:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    attempts = [r["attempts"] for r in counted if "attempts" in r]
    return {
        "mode": mode,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "total": len(counted),
        "accuracy": round(by_status.get("pass", 0) / len(counted), 4) if counted else None,
        # 별칭 이름만 다른 답까지 맞은 것으로 센 정확도
        "accuracy_alias_insensitive": round(
            (by_status.get("pass", 0) + by_status.get("pass_alias", 0)) / len(counted), 4) if counted else None,
        "by_status": by_status,
        "avg_attempts": round(sum(attempts) / len(attempts), 2) if attempts else None,
        "failures": [r for r in counted if r["status"] not in ("pass", "pass_alias")],
        "results": results,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="nl2sql 평가셋 실행기")
    parser.add_argument("--cases", required=True, type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--contract", type=Path, help="계약서 JSON 파일")
    source.add_argument("--datasource-id", help="메타데이터 DB 의 데이터소스 id")
    parser.add_argument("--compile-only", action="store_true", help="모델 없이 ast → expected_sql 만 확인")
    parser.add_argument("--out", type=Path, help="리포트 경로 (기본: eval/reports/<시각>.json)")
    args = parser.parse_args(argv)

    data = load_cases(args.cases)
    if args.contract:
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
    else:
        import db
        from contract import load_contract
        from uuid import UUID
        db.init_pool()
        try:
            contract = load_contract(UUID(args.datasource_id))
        finally:
            db.close_pool()

    today = datetime.date.fromisoformat(data["today"]) if data.get("today") else None
    report = run_compile_only(contract, data) if args.compile_only else run_model(contract, data, today)

    out = args.out or REPORT_DIR / f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{report['mode']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n정확도 {report['accuracy']} (별칭 무시 {report['accuracy_alias_insensitive']}) {report['by_status']} "
          f"· 평균 시도 {report['avg_attempts']} · 리포트 {out}")
    for f in report["failures"][:20]:
        print(f"  - [{f['status']}] {f['question']}: {f.get('error') or f.get('clarification') or f.get('sql')}")
    return 0 if report["accuracy"] == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
