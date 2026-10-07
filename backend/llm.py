import os

from langchain_openai import ChatOpenAI


def make_model(temperature: float = 0.0) -> ChatOpenAI:
    """OpenAI 호환 LLM (LLM_BASE_URL · LLM_API_KEY · LLM_MODEL)"""
    return ChatOpenAI(
        base_url=os.environ.get("LLM_BASE_URL"),
        api_key=os.environ.get("LLM_API_KEY", "not-needed"),
        model=os.environ.get("LLM_MODEL", "gpt-3.5-turbo"),
        temperature=temperature,
    )
