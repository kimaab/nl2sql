import json
import logging
from langchain_core.tools import tool
from compiler import Compiler

log = logging.getLogger("nl2sql.compile")


def make_compile_tool(compiler: Compiler, question: str):
    """compile_sql 도구 생성"""
    attempt = {"n": 0}

    @tool("compile_sql")
    def compile_sql(ast: str) -> str:
        """제안한 조회 명세(JSON AST)를 검증하고 SQL로 컴파일합니다.

        ast는 아래 키만 담은 JSON 객체를 문자열로 인코딩한 것입니다:
        target_table 또는 metric, aggregations, filters, group_by.

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

        try:
            sql = compiler.compile(parsed)
        except (ValueError, KeyError, TypeError) as error:
            log.info("attempt=%d outcome=error question=%r ast=%s detail=%s",
                     attempt["n"], question, payload, error)
            return f"error: {error}"

        log.info("attempt=%d outcome=success question=%r sql=%s",
                 attempt["n"], question, sql)
        return f"SQL: {sql}"

    return compile_sql
