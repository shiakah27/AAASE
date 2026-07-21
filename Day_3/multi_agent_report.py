import os
import operator
from datetime import datetime
from typing import Annotated, List
from typing_extensions import TypedDict

from dotenv import load_dotenv
from tavily import TavilyClient

from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

load_dotenv()  # loads .env keys into the environment


# shared memory passed between every agent
class AgentState(TypedDict):
    topic: str
    research_notes: str
    summary: str
    draft_report: str
    final_report: str
    execution_log: Annotated[List[str], operator.add]  # appends instead of overwriting


# LLM via OpenRouter (OpenAI-compatible endpoint)
llm = ChatOpenAI(
    model=os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"),
    temperature=0,
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1",
)

# web search client
tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))


def _log(msg: str) -> str:
    # one timestamped log line
    return f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"


def report_manager_start(state: AgentState):
    # coordinator: announces the pipeline is starting
    return {"execution_log": [_log(f"MANAGER starting pipeline for topic '{state['topic']}'")]}


def research_agent(state: AgentState):
    # searches the web and writes detailed research notes
    results = tavily.search(query=state["topic"], search_depth="advanced", max_results=6)
    sources_text = "\n\n".join(
        f"{r.get('title', 'Untitled')} ({r.get('url', '')})\n{r.get('content', '')}"
        for r in results.get("results", [])
    )

    prompt = f"""Write detailed research notes on "{state['topic']}" based on these sources:

{sources_text}

Cover the main facts, figures, and viewpoints found across all sources."""
    notes = llm.invoke(prompt).content

    return {
        "research_notes": notes,
        "execution_log": [_log(f"RESEARCH_AGENT produced notes from {len(results.get('results', []))} sources")],
    }


def summarization_agent(state: AgentState):
    # condenses research notes into a short summary
    prompt = f"""Condense these research notes into a short, clear summary
(under 200 words) that keeps only the most important points:

{state['research_notes']}"""
    summary = llm.invoke(prompt).content

    return {
        "summary": summary,
        "execution_log": [_log("SUMMARIZATION_AGENT produced short summary")],
    }


def writing_agent(state: AgentState):
    # turns the summary into a structured draft report
    prompt = f"""Using this summary, write a structured draft report on "{state['topic']}":

{state['summary']}

Format in Markdown with sections:
# Executive Summary
# Key Points
# Conclusion"""
    draft = llm.invoke(prompt).content

    return {
        "draft_report": draft,
        "execution_log": [_log("WRITING_AGENT produced draft report")],
    }


def review_agent(state: AgentState):
    # reviews and polishes the draft into the final report
    prompt = f"""Review and improve this draft report on "{state['topic']}".
Fix any unclear wording, tighten the structure, and make it professional.
Keep the same Markdown section headers.

Draft:
{state['draft_report']}"""
    final = llm.invoke(prompt).content

    return {
        "final_report": final,
        "execution_log": [_log("REVIEW_AGENT produced final polished report")],
    }


def report_manager_finish(state: AgentState):
    # coordinator: logs a summary of the whole run
    log = _log(
        f"MANAGER pipeline complete -- topic='{state['topic']}', "
        f"research={len(state['research_notes'])} chars, "
        f"summary={len(state['summary'])} chars, "
        f"final report={len(state['final_report'])} chars"
    )
    return {"execution_log": [log]}


# build the graph: manager -> research -> summarize -> write -> review -> manager -> END
workflow = StateGraph(AgentState)
workflow.add_node("manager_start", report_manager_start)
workflow.add_node("research", research_agent)
workflow.add_node("summarize", summarization_agent)
workflow.add_node("write", writing_agent)
workflow.add_node("review", review_agent)
workflow.add_node("manager_finish", report_manager_finish)

workflow.add_edge(START, "manager_start")
workflow.add_edge("manager_start", "research")
workflow.add_edge("research", "summarize")
workflow.add_edge("summarize", "write")
workflow.add_edge("write", "review")
workflow.add_edge("review", "manager_finish")
workflow.add_edge("manager_finish", END)


if __name__ == "__main__":
    app = workflow.compile(checkpointer=InMemorySaver())

    print(app.get_graph().draw_mermaid())  # paste into mermaid.live to view

    initial_state = {
        "topic": "The impact of AI agents on enterprise productivity",
        "research_notes": "",
        "summary": "",
        "draft_report": "",
        "final_report": "",
        "execution_log": [],
    }
    config = {"configurable": {"thread_id": "run-1"}}

    final_state = None
    for chunk in app.stream(initial_state, config, stream_mode="values"):
        final_state = chunk
        if chunk.get("execution_log"):
            print(chunk["execution_log"][-1])

    # show every agent's individual output, not just the final result
    print("\n===== RESEARCH NOTES =====\n")
    print(final_state["research_notes"])

    print("\n===== SUMMARY =====\n")
    print(final_state["summary"])

    print("\n===== DRAFT REPORT =====\n")
    print(final_state["draft_report"])

    print("\n===== FINAL REPORT =====\n")
    print(final_state["final_report"])

    with open("report_output.md", "w") as f:
        f.write(final_state["final_report"])
