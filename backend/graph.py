from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_core.messages import ToolMessage

MAX_ATTEMPTS = 4


class State(TypedDict):
    """에이전트 상태"""
    messages: Annotated[list, add_messages]


def build_graph(model, compile_tool):
    """LangGraph 에이전트 그래프 생성"""
    bound = model.bind_tools([compile_tool])

    def agent(state: State) -> dict:
        """모델 호출"""
        return {"messages": [bound.invoke(state["messages"])]}

    def route_after_agent(state: State) -> str:
        """에이전트 후 라우팅"""
        last = state["messages"][-1]
        return "compile" if getattr(last, "tool_calls", None) else END

    def route_after_tool(state: State) -> str:
        """도구 실행 후 라우팅"""
        last = state["messages"][-1]
        # 성공하면 여기서 바로 끝낸다
        if isinstance(last, ToolMessage) and str(last.content).startswith("SQL:"):
            return END
        # 최대 시도 횟수 체크
        attempts = sum(1 for m in state["messages"] if isinstance(m, ToolMessage))
        return END if attempts >= MAX_ATTEMPTS else "agent"

    graph = StateGraph(State)
    graph.add_node("agent", agent)
    graph.add_node("compile", ToolNode([compile_tool]))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"compile": "compile", END: END})
    graph.add_conditional_edges("compile", route_after_tool, {"agent": "agent", END: END})

    # 체크포인터 없음 — 요청 간 격리
    return graph.compile()
