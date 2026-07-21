import json
import logging
import operator
import os
import random
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, List
from typing_extensions import TypedDict

from dotenv import load_dotenv

from langgraph.graph import StateGraph, START, END

load_dotenv()

STAGE = int(os.getenv("LAB_STAGE", "0"))   # 0..5 -- maturity level
MOCK = os.getenv("MOCK", "0") == "1"


# ============================================================
# STEP 1 -- STATE (the contract between agents)
# ============================================================
class ReportState(TypedDict, total=False):
    run_id: str
    topic: str
    research_notes: str
    summary: str
    draft: str
    review_feedback: str
    score: int
    revision_count: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    error: str
    execution_logs: Annotated[List[str], operator.add]  # appends instead of overwriting


# ============================================================
# STEP 2 -- MODEL (with offline mock)
# ============================================================
class FakeResponse:
    def __init__(self, content):
        self.content = content
        self.usage_metadata = {"input_tokens": 200, "output_tokens": 300}


class FakeChatModel:
    def __init__(self):
        self.review_calls = 0

    def invoke(self, prompt: str):
        time.sleep(0.2)
        p = prompt.lower()
        if "reviewer" in p:
            self.review_calls += 1
            score = 6 if self.review_calls == 1 else 9
            return FakeResponse(f"SCORE: {score}\nFEEDBACK: Add a concrete example.")
        if "research" in p:
            return FakeResponse("- fact one\n- fact two\n- fact three")
        if "summar" in p:
            return FakeResponse("A concise summary of the research notes.")
        return FakeResponse(
            "INTRODUCTION\n...\n\nBODY\n" + "Substantive findings. " * 20 + "\n\nCONCLUSION\n..."
        )


def _build_model():
    # real model via OpenRouter, unless MOCK=1
    if MOCK:
        return FakeChatModel()
    from langchain_openai import ChatOpenAI

    kwargs = dict(
        model=os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"),
        temperature=0,
        api_key=os.getenv("OPENROUTER_API_KEY"),
        base_url="https://openrouter.ai/api/v1",
    )
    if STAGE >= 1:
        # we own retries ourselves in call_llm -- disable the SDK's so they don't stack
        kwargs["timeout"] = 60
        kwargs["max_retries"] = 0
    return ChatOpenAI(**kwargs)


model = _build_model()


# ============================================================
# STEP 6 -- STAGE 2: CONFIG & SECRETS (defined early so nodes can use it)
# ============================================================
@dataclass
class Settings:
    model_name: str = "nvidia/nemotron-3-super-120b-a12b:free"
    temperature: float = 0.0
    request_timeout_s: int = 60
    max_retries: int = 3
    quality_threshold: int = 8
    max_revisions: int = 2
    cost_budget_usd: float = 1.0
    max_topic_len: int = 300

    @classmethod
    def from_env(cls):
        d = cls()
        return cls(
            model_name=os.getenv("MODEL_NAME", d.model_name),
            temperature=float(os.getenv("TEMPERATURE", d.temperature)),
            request_timeout_s=int(os.getenv("REQUEST_TIMEOUT_S", d.request_timeout_s)),
            max_retries=int(os.getenv("MAX_RETRIES", d.max_retries)),
            quality_threshold=int(os.getenv("QUALITY_THRESHOLD", d.quality_threshold)),
            max_revisions=int(os.getenv("MAX_REVISIONS", d.max_revisions)),
            cost_budget_usd=float(os.getenv("COST_BUDGET_USD", d.cost_budget_usd)),
            max_topic_len=int(os.getenv("MAX_TOPIC_LEN", d.max_topic_len)),
        )


settings = Settings.from_env() if STAGE >= 2 else Settings()


# ============================================================
# STEP 7 -- STAGE 3: OBSERVABILITY (defined early so call_llm can use it)
# ============================================================
def _configure_logger():
    logger = logging.getLogger("enterprise_agent")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))  # message IS the json
        logger.addHandler(handler)
    return logger


_logger = _configure_logger()


def log_event(event: str, **fields):
    # one JSON object per line, always includes a timestamp + event name
    record = {"ts": datetime.now(timezone.utc).isoformat(), "level": "INFO", "event": event, **fields}
    _logger.info(json.dumps(record))


# ============================================================
# STEP 8 -- STAGE 4: GUARDRAILS + COST (defined early so call_llm can use it)
# ============================================================
class BudgetExceeded(Exception):
    pass


INJECTION_PATTERNS = [
    r"ignore (all|previous|the) instructions",
    r"disregard (all|previous|the) instructions",
    r"system prompt",
]
REFUSAL_ARTIFACTS = ["as an ai language model", "i cannot assist", "i'm sorry, but i can't"]


def validate_topic(topic: str) -> str:
    # rejects empty, too-long, or prompt-injection style topics
    if not topic or not topic.strip():
        raise ValueError("Topic cannot be empty")
    if len(topic) > settings.max_topic_len:
        raise ValueError(f"Topic exceeds max length of {settings.max_topic_len}")
    lowered = topic.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, lowered):
            raise ValueError("Topic contains a disallowed instruction-override pattern")
    return topic.strip()


def validate_report(report: str) -> None:
    # rejects too-short reports or ones containing refusal artifacts
    if not report or len(report) < 200:
        raise ValueError("Report is too short to be valid")
    lowered = report.lower()
    for artifact in REFUSAL_ARTIFACTS:
        if artifact in lowered:
            raise ValueError("Report contains a refusal artifact")


# ============================================================
# STEP 5 -- STAGE 1: ROBUSTNESS -- the one chokepoint for every LLM call
# ============================================================
def call_llm(prompt: str, node: str, state: ReportState) -> str:
    if STAGE >= 4 and state.get("cost_usd", 0) >= settings.cost_budget_usd:
        raise BudgetExceeded(f"Cost budget (${settings.cost_budget_usd}) exceeded before calling '{node}'")

    attempts = settings.max_retries if STAGE >= 1 else 1
    last_error = None

    for attempt in range(1, attempts + 1):
        start = time.time()
        try:
            response = model.invoke(prompt)
            latency = time.time() - start

            usage = getattr(response, "usage_metadata", None) or {}
            tokens_in = usage.get("input_tokens", 0)
            tokens_out = usage.get("output_tokens", 0)
            cost = (tokens_in + tokens_out) / 1000 * 0.002  # rough per-1k-token estimate

            state["tokens_in"] = state.get("tokens_in", 0) + tokens_in
            state["tokens_out"] = state.get("tokens_out", 0) + tokens_out
            state["cost_usd"] = state.get("cost_usd", 0) + cost

            if STAGE >= 3:
                log_event(
                    "llm_call", run_id=state.get("run_id"), node=node, attempt=attempt,
                    latency_s=round(latency, 3), tokens_in=tokens_in, tokens_out=tokens_out,
                    cost_usd=round(cost, 6),
                )
            return response.content

        except Exception as e:
            last_error = e
            if STAGE >= 3:
                log_event("llm_retry", run_id=state.get("run_id"), node=node, attempt=attempt, error=str(e))
            if attempt < attempts:
                delay = 2 ** (attempt - 1) + random.uniform(0, 0.5)  # backoff + jitter
                time.sleep(delay)

    raise RuntimeError(f"'{node}' failed after {attempts} attempt(s): {last_error}")


# ============================================================
# STEP 3 -- ROLE-SPECIALIZED AGENTS
# ============================================================
def research_node(state: ReportState):
    prompt = f"You are a Researcher. Research the topic '{state['topic']}'. List the key facts as bullet points."
    content = call_llm(prompt, "research", state)
    return {
        "research_notes": content,
        "tokens_in": state["tokens_in"], "tokens_out": state["tokens_out"], "cost_usd": state["cost_usd"],
        "execution_logs": ["RESEARCH produced notes"],
    }


def summarize_node(state: ReportState):
    prompt = f"You are a Summarizer. Condense these notes into a short summary:\n\n{state['research_notes']}"
    content = call_llm(prompt, "summarize", state)
    return {
        "summary": content,
        "tokens_in": state["tokens_in"], "tokens_out": state["tokens_out"], "cost_usd": state["cost_usd"],
        "execution_logs": ["SUMMARIZE produced summary"],
    }


def write_node(state: ReportState):
    feedback_line = ""
    if state.get("review_feedback"):
        feedback_line = f"\n\nPrior feedback to address: {state['review_feedback']}."
    prompt = (
        f"You are a Writer. Write a full report on '{state['topic']}' using these notes:\n\n"
        f"{state['summary']}{feedback_line}\n\n"
        f"Do not include a signature block, author name, organization name, or "
        f"'prepared by' section -- end the report after the conclusion."
    )
    content = call_llm(prompt, "write", state)
    return {
        "draft": content,
        "tokens_in": state["tokens_in"], "tokens_out": state["tokens_out"], "cost_usd": state["cost_usd"],
        "execution_logs": ["WRITE produced draft"],
    }


def review_node(state: ReportState):
    prompt = (
        f"You are a Reviewer. Score this draft 1-10 on quality and give one line of feedback.\n"
        f"Respond exactly as:\nSCORE: <number>\nFEEDBACK: <one line>\n\nDraft:\n{state['draft']}"
    )
    content = call_llm(prompt, "review", state)

    score_match = re.search(r"SCORE:\s*(\d+)", content)
    feedback_match = re.search(r"FEEDBACK:\s*(.+)", content)
    score = int(score_match.group(1)) if score_match else 0
    feedback = feedback_match.group(1).strip() if feedback_match else ""
    revision_count = state.get("revision_count", 0) + 1

    if STAGE >= 3:
        log_event("review_verdict", run_id=state.get("run_id"), score=score, revision_count=revision_count)

    return {
        "score": score,
        "review_feedback": feedback,
        "revision_count": revision_count,
        "tokens_in": state["tokens_in"], "tokens_out": state["tokens_out"], "cost_usd": state["cost_usd"],
        "execution_logs": [f"REVIEW score={score} revision_count={revision_count}"],
    }


# ============================================================
# STEP 4 -- SUPERVISOR DECISION
# ============================================================
def review_gate(state: ReportState) -> str:
    if state["score"] >= settings.quality_threshold:
        return "approve"
    if state["revision_count"] > settings.max_revisions:
        return "give_up"
    return "revise"


workflow = StateGraph(ReportState)
workflow.add_node("research", research_node)
workflow.add_node("summarize", summarize_node)
workflow.add_node("write", write_node)
workflow.add_node("review", review_node)

workflow.add_edge(START, "research")
workflow.add_edge("research", "summarize")
workflow.add_edge("summarize", "write")
workflow.add_edge("write", "review")
workflow.add_conditional_edges("review", review_gate, {"approve": END, "give_up": END, "revise": "write"})

graph = workflow.compile()


# ============================================================
# STEP 9 -- generate_report(): ties every stage together
# ============================================================
def generate_report(topic: str) -> ReportState:
    run_id = str(uuid.uuid4())
    state: ReportState = {
        "run_id": run_id, "topic": topic, "research_notes": "", "summary": "", "draft": "",
        "review_feedback": "", "score": 0, "revision_count": 0,
        "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "error": "", "execution_logs": [],
    }

    if STAGE >= 4:
        state["topic"] = validate_topic(topic)  # raises ValueError -- caller decides how to handle

    if STAGE >= 3:
        log_event("run_started", run_id=run_id, topic=state["topic"])

    try:
        final = graph.invoke(state)
    except (RuntimeError, BudgetExceeded) as e:
        if STAGE >= 1:
            state["error"] = str(e)
            if STAGE >= 3:
                log_event("run_finished", run_id=run_id, status="error", error=str(e))
            return state
        raise  # Stage 0: prototypes just crash

    if STAGE >= 4:
        validate_report(final.get("draft", ""))  # raises ValueError if invalid

    if STAGE >= 3:
        log_event(
            "run_finished", run_id=run_id, status="ok", score=final.get("score"),
            revision_count=final.get("revision_count"), tokens_in=final.get("tokens_in"),
            tokens_out=final.get("tokens_out"), cost_usd=round(final.get("cost_usd", 0), 6),
        )

    return final


# ============================================================
# STEP 10 -- STAGE 5: SERVING
# ============================================================
def create_app():
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel

    class ReportRequest(BaseModel):
        topic: str

    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok", "stage": STAGE, "model": settings.model_name}

    @app.post("/report")
    def report(req: ReportRequest):
        try:
            result = generate_report(req.topic)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if result.get("error"):
            raise HTTPException(status_code=503, detail=result["error"])
        return result

    return app


if __name__ == "__main__":
    print(f"=== STAGE {STAGE} {'(MOCK)' if MOCK else ''} ===")
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        import uvicorn
        uvicorn.run(create_app(), host="0.0.0.0", port=8000)
    else:
        topic = os.getenv("TOPIC", "The impact of AI agents on enterprise productivity")
        try:
            result = generate_report(topic)
        except ValueError as e:
            print(f"REJECTED: {e}")
            sys.exit(1)

        print(f"\nSCORE: {result.get('score')}  REVISIONS: {result.get('revision_count')}  "
              f"COST: ${result.get('cost_usd', 0):.6f}")
        if result.get("error"):
            print(f"ERROR: {result['error']}")
        print("\n--- DRAFT ---\n")
        print(result.get("draft", ""))

        with open("report_output.md", "w") as f:
            f.write(f"# {result.get('topic')}\n\n")
            f.write(f"Score: {result.get('score')}/10  \n")
            f.write(f"Revisions: {result.get('revision_count')}  \n")
            f.write(f"Cost: ${result.get('cost_usd', 0):.6f}\n\n---\n\n")
            f.write(result.get("draft", ""))
        print("\nSaved: report_output.md")