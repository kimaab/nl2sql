import os

from langchain_openai import ChatOpenAI


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
    )
