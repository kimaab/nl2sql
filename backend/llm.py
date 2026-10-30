import os

from langchain_openai import ChatOpenAI


def no_thinking(model: str) -> dict:
    """vLLM 요청 본문에 실을 값: 추론(thinking)을 끈다.
    서버마다 기본값이 달라, 켜진 서버는 답보다 몇 배 많은 토큰을 추론에 쓰고 그 내용은 응답에 남지 않는다.
    LLM_THINKING=on 이면 서버 기본값을 따른다. Claude 는 이 값을 모르니 싣지 않는다."""
    if model.startswith("claude") or os.environ.get("LLM_THINKING", "").lower() == "on":
        return {}
    # if  1==1 :
    #     return {}

    return {"chat_template_kwargs": {"enable_thinking": False}}


def make_model(temperature: float = 0.0) -> ChatOpenAI:
    """OpenAI 호환 LLM (LLM_BASE_URL · LLM_API_KEY · LLM_MODEL)"""
    model = os.environ.get("LLM_MODEL", "gpt-3.5-turbo")
    print(f"q url: {os.environ.get("LLM_BASE_URL")}\n모델: {model}\n")

    # Claude 는 temperature 를 보내면 400 이다 — None 이면 요청에서 빠진다
    if model.startswith("claude"):
        temperature = None
    return ChatOpenAI(
        base_url=os.environ.get("LLM_BASE_URL"),
        api_key=os.environ.get("LLM_API_KEY", "not-needed"),
        model=model,
        temperature=temperature,
        extra_body=no_thinking(model) or None,
    )
