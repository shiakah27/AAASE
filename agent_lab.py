import os
import operator
from datetime import datetime
from typing import Annotated, List, Dict
from typing_extensions import TypedDict

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_huggingface import HuggingFaceEmbeddings

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

load_dotenv()  # loads .env keys into the environment


# shared memory passed between every step of the graph
class AgentState(TypedDict):
    topic: str
    search_query: str
    collected_data: List[Dict]
    analyzed_data: List[Dict]
    quality_score: int
    iteration_count: int
    final_report: str
    execution_logs: Annotated[List[str], operator.add]  # appends instead of overwriting


# LLM via OpenRouter (OpenAI-compatible endpoint)
llm = ChatOpenAI(
    model=os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"),
    temperature=0,
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1",
)

# web search tool
search_tool = TavilySearch(max_results=5, tavily_api_key=os.getenv("TAVILY_API_KEY"))

# local embedding model + in-memory vector store (OpenRouter has no embeddings endpoint)
embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
vector_store = InMemoryVectorStore(embeddings)


# forces the LLM to return a real number + reason, never free text
class QualityScore(BaseModel):
    score: int = Field(ge=1, le=10)
    reasoning: str = Field(description="One-sentence justification")


evaluator = llm.with_structured_output(QualityScore)


def _log(msg: str) -> str:
    # one timestamped log line
    return f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"


def collect_node(state: AgentState):
    # searches the web; builds a new query each retry instead of repeating the same one
    iteration = state["iteration_count"] + 1

    if iteration == 1:
        query = state["topic"]
    else:
        prior_findings = "\n".join(
            f"{a['title']}: {a['analysis']}" for a in state["analyzed_data"]
        )[:1500]
        prompt = f"""Topic: "{state['topic']}"
Previous query: "{state['search_query']}"
Learned so far: {prior_findings}

Give ONE new, more specific search query to fill a gap. Reply with only the query."""
        query = llm.invoke(prompt).content.strip().strip('"')

    raw = search_tool.invoke({"query": query})
    results = raw.get("results", []) if isinstance(raw, dict) else []
    collected = [
        {"title": r.get("title", "Untitled"), "url": r.get("url", ""), "content": r.get("content", "")}
        for r in results
    ]

    return {
        "search_query": query,
        "collected_data": collected,
        "iteration_count": iteration,
        "execution_logs": [_log(f"COLLECT iter={iteration} query='{query}' found={len(collected)} sources")],
    }


def store_memory_node(state: AgentState):
    # saves this round's sources into the vector store
    docs = state["collected_data"]
    if docs:
        texts = [f"{d['title']}\n{d['content']}" for d in docs]
        metadatas = [{"url": d["url"], "topic": state["topic"]} for d in docs]
        vector_store.add_texts(texts, metadatas=metadatas)

    return {"execution_logs": [_log(f"STORE_MEMORY saved {len(docs)} source(s)")]}


def analyze_node(state: AgentState):
    # summarizes each source, pulling in related past findings from memory (RAG)
    analyzed = []
    for item in state["collected_data"]:
        related = vector_store.similarity_search(item["content"][:500], k=2)
        related_text = "\n".join(d.page_content[:300] for d in related)

        prompt = f"""Source about "{state['topic']}":
Title: {item['title']}
Content: {item['content'][:1200]}

Related past research: {related_text if related_text else "none yet"}

Write 2-4 sentences on what this source contributes."""
        response = llm.invoke(prompt)
        analyzed.append({"title": item["title"], "url": item["url"], "analysis": response.content})

    return {
        "analyzed_data": analyzed,
        "execution_logs": [_log(f"ANALYZE processed {len(analyzed)} source(s)")],
    }


def evaluate_node(state: AgentState):
    # scores research quality via structured output, never parsed from free text
    combined = "\n\n".join(f"{a['title']}: {a['analysis']}" for a in state["analyzed_data"])
    prompt = f"""Rate the thoroughness of this research on "{state['topic']}":

{combined}"""
    result: QualityScore = evaluator.invoke(prompt)

    return {
        "quality_score": result.score,
        "execution_logs": [_log(f"EVALUATE score={result.score}/10 -- {result.reasoning}")],
    }


def report_node(state: AgentState):
    # writes the final structured Markdown report
    combined = "\n\n".join(f"### {a['title']} ({a['url']})\n{a['analysis']}" for a in state["analyzed_data"])
    prompt = f"""Write a structured enterprise research report on "{state['topic']}"
using this analysis:

{combined}

Format in Markdown with these sections:
# Executive Summary
# Key Findings
# Detailed Analysis
# Sources
"""
    response = llm.invoke(prompt)
    report = response.content + f"\n\n# Quality Score\n{state['quality_score']}/10\n"

    return {
        "final_report": report,
        "execution_logs": [_log("REPORT generated final report")],
    }


def audit_node(state: AgentState):
    # final one-line summary of the whole run
    log = _log(
        f"AUDIT complete -- topic='{state['topic']}', {state['iteration_count']} iteration(s), "
        f"final quality={state['quality_score']}/10, {len(state['collected_data'])} source(s) in final round"
    )
    return {"execution_logs": [log]}


def quality_router(state: AgentState) -> str:
    # loop back on low quality, but stop after 3 tries no matter what
    if state["quality_score"] >= 7 or state["iteration_count"] >= 3:
        return "report"
    return "collect"


# build the graph: collect -> store_memory -> analyze -> evaluate -> (loop or report) -> audit -> END
workflow = StateGraph(AgentState)
workflow.add_node("collect", collect_node)
workflow.add_node("store_memory", store_memory_node)
workflow.add_node("analyze", analyze_node)
workflow.add_node("evaluate", evaluate_node)
workflow.add_node("report", report_node)
workflow.add_node("audit", audit_node)

workflow.add_edge(START, "collect")
workflow.add_edge("collect", "store_memory")
workflow.add_edge("store_memory", "analyze")
workflow.add_edge("analyze", "evaluate")
workflow.add_conditional_edges("evaluate", quality_router, {"collect": "collect", "report": "report"})
workflow.add_edge("report", "audit")
workflow.add_edge("audit", END)


if __name__ == "__main__":
    app = workflow.compile(checkpointer=InMemorySaver())

    print(app.get_graph().draw_mermaid())  # paste into mermaid.live to view

    initial_state = {
        "topic": "Enterprise Agentic AI Systems",
        "search_query": "",
        "collected_data": [],
        "analyzed_data": [],
        "quality_score": 0,
        "iteration_count": 0,
        "final_report": "",
        "execution_logs": [],
    }
    config = {"configurable": {"thread_id": "run-1"}}

    final_state = None
    for chunk in app.stream(initial_state, config, stream_mode="values"):
        final_state = chunk
        if chunk.get("execution_logs"):
            print(chunk["execution_logs"][-1])

    print("\n===== FINAL REPORT =====\n")
    print(final_state["final_report"])

    with open("report_output.md", "w") as f:
        f.write(final_state["final_report"])