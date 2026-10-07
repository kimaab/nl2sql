import json
import logging
from langchain_core.tools import tool
from compiler import Compiler

log = logging.getLogger("nl2sql.compile")

CLARIFY_PREFIX = "CLARIFY:"


def make_compile_tool(compiler: Compiler, question: str, record: dict | None = None,
                      expected_metrics: list | None = None):
    """compile_sql 도구 생성. 성공하면 record["ast"] 에 컴파일된 AST 를 남긴다.

    expected_metrics: 지표 선택 단계가 고른 지표. 첫 시도가 그 지표를 쓰지 않으면 한 번 되돌려 보낸다 —
    지표를 두고 원본 컬럼을 직접 집계하면 오류 없이 틀린 답이 나온다. 두 번째부터는 모델 판단을 따른다.
    """
    attempt = {"n": 0}
    expected = [m.lower() for m in expected_metrics or []]

    @tool("compile_sql")
    def compile_sql(ast: str) -> str:
        """제안한 조회 명세(JSON AST)를 검증하고 SQL로 컴파일합니다.

        ast는 아래 키만 담은 JSON 객체를 문자열로 인코딩한 것입니다:
        metric 또는 target_table, columns, joins, aggregations, filters, group_by,
        having, order_by, limit, distinct, compare.

        성공하면 "SQL: "로 시작하는 문자열을 돌려줍니다 — 그것이 최종 답이니
        이 도구를 더 부르지 말고 답변을 마치십시오.
        실패하면 "error: "로 시작하는 문자열을 돌려줍니다 — 그 메시지가 무엇을
        고쳐야 하는지 말해주고 있으니, 읽고 고쳐서 다시 이 도구를 부르십시오.
        """
        attempt["n"] += 1

        payload = ast.strip()
        # 모델이 코드펜스를 붙여 보내는 일이 흔하다
        if payload.startswith("```"):
            payload = payload.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as error:
            detail = f"ast가 올바른 JSON이 아닙니다 -> {error}"
            log.info("attempt=%d outcome=error question=%r detail=%s",
                     attempt["n"], question, detail)
            return f"error: {detail}"

        if not isinstance(parsed, dict):
            detail = f"ast는 JSON 객체여야 합니다 (받은 것: {type(parsed).__name__})"
            log.info("attempt=%d outcome=error question=%r detail=%s",
                     attempt["n"], question, detail)
            return f"error: {detail}"

        used = parsed.get("metric")
        used = [used] if isinstance(used, str) else used if isinstance(used, list) else []
        if expected and attempt["n"] == 1 and not any(str(u).lower() in expected for u in used):
            detail = (f"이 질문에는 지표 선택 단계에서 {', '.join(expected_metrics)} 을(를) 골랐습니다. "
                      "metric 에 그 지표를 쓰고 정렬·기간·필터만 덧붙이십시오. 지표로 정말 답할 수 없을 때만 "
                      "같은 ast 를 다시 보내십시오.")
            log.info("attempt=%d outcome=error question=%r detail=지표 미사용 %s", attempt["n"], question, payload)
            return f"error: {detail}"

        try:
            sql = compiler.compile(parsed)
        except (ValueError, KeyError, TypeError) as error:
            log.info("attempt=%d outcome=error question=%r ast=%s detail=%s",
                     attempt["n"], question, payload, error)
            return f"error: {error}"

        if record is not None:
            record["ast"] = parsed
        log.info("attempt=%d outcome=success question=%r sql=%s",
                 attempt["n"], question, sql)
        return f"SQL: {sql}"

    return compile_sql


def make_clarify_tool(question: str):
    """ask_user 도구 생성. 부르면 이번 요청은 SQL 없이 되묻는 말로 끝난다."""

    @tool("ask_user")
    def ask_user(message: str) -> str:
        """질문만으로는 조회를 정할 수 없을 때 사용자에게 한 번 되묻습니다.

        어떤 지표·테이블을 말하는지 여러 갈래로 읽히거나, 지표가 질문에서 받아야 하는
        값(예: 어느 회원인지)이 빠졌을 때만 씁니다. 전체 조회나 합리적인 기본값으로
        답할 수 있으면 되묻지 말고 compile_sql 을 부르십시오.
        message 에는 사용자에게 보여줄 짧은 질문 한 문장을 적습니다.
        """
        text = str(message or "").strip() or "질문을 조금 더 구체적으로 적어 주십시오."
        log.info("outcome=clarify question=%r message=%s", question, text)
        return f"{CLARIFY_PREFIX} {text}"

    return ask_user
