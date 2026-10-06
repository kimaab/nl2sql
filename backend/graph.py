from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_core.messages import ToolMessage

from tool import CLARIFY_PREFIX

MAX_ATTEMPTS = 4


class State(TypedDict):
    """에이전트 상태"""
    messages: Annotated[list, add_messages]


def last_tool_batch(messages: list) -> list[ToolMessage]:
    """마지막 모델 호출이 부른 도구들의 결과 (한 번에 여러 도구를 부를 수 있다)."""
    batch = []
    for m in reversed(messages):
        if not isinstance(m, ToolMessage):
            break
        batch.append(m)
    return list(reversed(batch))


def build_graph(model, tools: list):
    """LangGraph 에이전트 그래프 생성"""
    bound = model.bind_tools(tools)

    def agent(state: State) -> dict:
        """모델 호출"""
        return {"messages": [bound.invoke(state["messages"])]}

    def route_after_agent(state: State) -> str:
        """에이전트 후 라우팅"""
        last = state["messages"][-1]
        return "compile" if getattr(last, "tool_calls", None) else END

    def route_after_tool(state: State) -> str:
        """도구 실행 후 라우팅"""
        batch = last_tool_batch(state["messages"])
        # 성공하거나 되물으면 여기서 바로 끝낸다
        if any(str(m.content).startswith(("SQL:", CLARIFY_PREFIX)) for m in batch):
            return END
        # 최대 시도 횟수 체크 — 되묻기는 시도가 아니다
        attempts = sum(
            1 for m in state["messages"]
            if isinstance(m, ToolMessage) and not str(m.content).startswith(CLARIFY_PREFIX)
        )
        return END if attempts >= MAX_ATTEMPTS else "agent"

    graph = StateGraph(State)
    graph.add_node("agent", agent)
    graph.add_node("compile", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"compile": "compile", END: END})
    graph.add_conditional_edges("compile", route_after_tool, {"agent": "agent", END: END})

    # 체크포인터 없음 — 요청 간 격리
    return graph.compile()
