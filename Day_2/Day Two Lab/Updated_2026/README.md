# Enterprise Autonomous Research AI Agent — Day 2 Lab (2026 revision)

An autonomous research agent built as a **LangGraph state graph**: it searches the web, stores findings in vector memory, analyzes sources with an LLM, grades its own research quality, and **loops back to re-search with a refined query** if quality is too low — then writes an enterprise report.

## What changed vs. the original lab

| # | Change | Why |
|---|--------|-----|
| 1 | `langchain-tavily` (`TavilySearch`), `langchain_openai.OpenAIEmbeddings`, `langchain_chroma.Chroma` | The old imports (`langchain.embeddings`, `langchain.vectorstores`, `langchain_community.tools.tavily_search`) are deprecated/removed and crash on a fresh install |
| 2 | Loop guard: `iteration_count` + query refinement + `MAX_RESEARCH_ITERATIONS` | The original looped back to the *same* search on low quality → same results → same score → infinite loop → `GraphRecursionError` |
| 3 | Structured output (Pydantic `QualityScore`) | `int(response.content)` breaks when the model answers "8/10" |
| 4 | Nodes return **partial state updates**; logs use an `operator.add` reducer | Idiomatic LangGraph — required for checkpointing and parallelism |
| 5 | Analysis node now **reads** from Chroma (`similarity_search`) | Original wrote to memory but never retrieved — not actually RAG |
| 6 | `InMemorySaver` checkpointer + optional `interrupt_before` | Slides promise checkpointing & human-in-the-loop; now the code shows it |
| 7 | **FAKE MODE** (`USE_FAKE=1`) | Runs the entire graph with zero API keys |

## Workflow

```
START → research_collection → memory_storage → analysis → quality_evaluation
              ↑                                                  │
              └── score < 7 and iterations < 3 ──────────────────┤
                                                                 └─ score ≥ 7 (or max
                                                                    iterations) →
                                                    report_generation → audit → END
```

The script also prints the graph as Mermaid — paste it into https://mermaid.live to see it.

## Installation

Python 3.10+

```bash
pip install langgraph langchain langchain-openai langchain-tavily python-dotenv

# Optional — persistent vector memory (heavy install, can take several minutes):
pip install langchain-chroma
```

If `langchain-chroma` isn't installed, the lab automatically falls back to LangChain's built-in `InMemoryVectorStore` (same API, memory just doesn't persist between runs). Useful when classroom machines or Wi-Fi can't handle the chromadb install.

## Running — three options

### Option A: Fake mode (no keys, free, offline)

```bash
USE_FAKE=1 python enterprise_research_agent.py
# Windows PowerShell:  $env:USE_FAKE="1"; python enterprise_research_agent.py
```

Uses `GenericFakeChatModel`, `DeterministicFakeEmbedding`, and a canned search fixture. Quality scores are scripted (4, then 9) so you can **watch the conditional edge loop exactly once** and then proceed. Ideal for testing routing logic and for classrooms without keys.

### Option B: Free-tier providers (recommended for students)

The Tavily free tier gives 1,000 search credits/month — no credit card (sign up at https://tavily.com, key starts with `tvly-`).

For the LLM, any OpenAI-compatible free tier works. Create `.env`:

```env
TAVILY_API_KEY=tvly-...

# OpenRouter (free NVIDIA Nemotron — openrouter.ai/keys):
OPENAI_API_KEY=sk-or-...
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=nvidia/nemotron-3-super-120b-a12b:free
# rate-limited? try: nvidia/nemotron-3-nano-30b-a3b:free

# Or Groq (free, fast Llama — console.groq.com):
# OPENAI_API_KEY=gsk_...
# LLM_BASE_URL=https://api.groq.com/openai/v1
# LLM_MODEL=llama-3.3-70b-versatile

# Or Google AI Studio (free, ~1500 req/day — aistudio.google.com):
# OPENAI_API_KEY=AIza...
# LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
# LLM_MODEL=gemini-2.5-flash

# Or Cerebras / OpenRouter free models — same pattern.
```

Note: with a non-OpenAI provider, embeddings still need an OpenAI key. Either run fake mode, use OpenAI for everything, or swap `OpenAIEmbeddings` for a local `HuggingFaceEmbeddings` (`pip install langchain-huggingface sentence-transformers`).

### Option C: OpenAI

```env
OPENAI_API_KEY=sk-...
TAVILY_API_KEY=tvly-...
```

```bash
python enterprise_research_agent.py
```

## Exercises

1. **Break it on purpose**: set `MAX_RESEARCH_ITERATIONS = 999` and force fake scores to always be 4. What error do you get, and after how many steps? (Hint: LangGraph's default recursion limit is 25.)
2. **Human-in-the-loop**: uncomment `interrupt_before=["report_generation"]` and resume the run from the checkpoint.
3. **Better refinement**: replace the hardcoded query refinements with an LLM call that rewrites the query based on *why* quality was low (use the `reasoning` field of `QualityScore`).
4. **Real RAG**: change the analysis prompt to cite which memory snippets it used.

## Expected output

Mermaid graph definition, timestamped execution logs (watch the iteration counter), a quality score with reasoning, and the final report. In fake mode you should see exactly two research iterations: score 4 → loop → score 9 → report.
