# Context Proxy MCP

![License](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.10+-blue.svg)

**Offload context management to cheap models. Let expensive reasoning models focus on thinking.**

---

## Why?

In a recent multi-model group chat session, we saw this API bill:

| Model | Cost (USD) | Share | Calls | Cache Hit Rate |
|---|---|---|---|---|
| Claude Opus | $18.51 | 46% | 162 | 0% |
| GPT-5.5 | $11.76 | 29% | 359 | 83% |
| DeepSeek V4 Pro | $7.22 | 18% | 559 | 62% |
| Gemini 3.1 Pro | $3.17 | 8% | 175 | 53% |

- Opus consumed **46%** of the budget with a **0%** cache hit rate.
- **85%** of its calls produced under 1,000 tokens — pure context management overhead, not deep reasoning.

**The most expensive model was wasted on the simplest task: remembering what was said before.**

---

## Core Insight

Expensive models don't need context. They need to reason.

Context Proxy completely strips "context management" from reasoning models and outsources it to a cheap, dedicated memory service (DeepSeek V4 Flash). It handles:

- **Compression** – DeepSeek distills verbose chat history into concise summaries (full history is kept in DeepSeek, not in Opus).
- **Storage** – Complete conversation history lives in DeepSeek's 1M token window (append-only).
- **Retrieval** – Semantic search over local long-term memory, plus on-demand deep search into raw history.
- **Cache Control** – Automatic management of OpenRouter's free response cache — identical queries don't trigger duplicate charges, and stale results are never returned.

---

## Key Advantages

- **100x cheaper context management** – DeepSeek V4 Flash output pricing at ¥2/million tokens vs Opus at $75/million tokens.
- **Zero context tax for reasoning models** – They only see the compact summary needed at this moment.
- **Local, searchable, permanent memory** – Long-term facts preserved across sessions.
- **Single-agent and multi-agent support** – Shared memory body, private working context.

---

## How It Works (30-second overview)

1. AI needs to remember something → calls `remember()`, submits raw content. DeepSeek compresses into a summary, returns it to the AI, and keeps the full version in its own context.
2. AI needs to recall → calls `recall()`. First searches local search engine (Chroma). If insufficient, deep-dives into DeepSeek's full history.
3. Workflow completes → DeepSeek extracts key points from the entire session, stores them in local search engine for permanent retention.
4. New session starts → AI pulls recent key memories in seconds, restoring context without replaying history.

---

## Architecture (Three-Layer Memory)

| Layer | Location | Lifecycle | Content |
|---|---|---|---|
| Working Memory | AI's own context window | Cleared after each task | Compressed summaries + retrieved fragments |
| Full History | DeepSeek (cloud) | Kept during workflow, then distilled | Complete conversation and thoughts |
| Long-term Memory | Local search engine (Chroma) | Permanent, cross-session | Key decisions, facts, conclusions |

**DeepSeek's dual role:**

- **Compressor / Distiller** (handles `remember` and `summarize_workflow`)
- **Full history storage + deep retrieval** (handles `recall`)

---

## Installation

```bash
git clone https://github.com/yourname/context-proxy-mcp.git
cd context-proxy-mcp
pip install -e .
```

Set API keys:

```bash
export OPENROUTER_API_KEY="sk-or-..."
export DEEPSEEK_API_KEY="sk-..."   # If connecting directly to DeepSeek
```

> Note: This project uses OpenRouter as the API gateway for DeepSeek. You can also connect directly to the DeepSeek official API.

---

## Quick Start (MCP Client)

Add the following to your Claude Desktop `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "context-proxy": {
      "command": "python",
      "args": ["-m", "context_proxy_mcp.server"],
      "env": {
        "OPENROUTER_API_KEY": "your-key-here"
      }
    }
  }
}
```

Now your AI can use these MCP tools:

| Tool | Description |
|---|---|
| `remember` | Submit content for compression; DeepSeek returns a summary and stores the original. |
| `recall` | Search local long-term memory, fall back to DeepSeek full history if needed. |
| `catch` | Retrieve recent key memories (fast context recovery). |
| `forget` | Delete specific memory entries. |
| `summarize_workflow` | End-of-workflow call to distill the session into permanent memory. |

---

## Usage Patterns

### Single AI

One agent manages its own memory. All memory layers are private.

### Multi-Agent Group Chat

Multiple agents share the same Context Proxy.

- **Shared**: DeepSeek full history, local search engine (long-term memory).
- **Private**: Each agent's working memory (compressed summaries).

Agent routing (inspired by OpenHanako):

- Agents silently listen, only speak when @mentioned.
- Only mentioned agents query memory — no broadcast amplification overhead.

---

## Real Cost Comparison

| Task | DeepSeek V4 Flash | Claude Opus | Savings |
|---|---|---|---|
| Compress 10K tokens | ~$0.0028 | ~$0.75 | ~270x |
| Store 1M tokens | ~$2 (holding full context) | ~$75 (full price per query) | ~37x |
| Frequent retrieval | Near zero (using cache) | $0.15+ per query | — |

This architecture works because DeepSeek is cheap enough to serve as a dedicated "memory processing unit."

---

## Hard Rules (Design Invariants)

1. **Expensive models never do context management** — they only reason. Compression and retrieval are always handled by DeepSeek.
2. **Compression happens on DeepSeek** — because the full context is there, compression is both more accurate and extremely cheap.
3. **@mentions in group chat only trigger mentioned agents** — avoids N-fold query amplification.

---

## Project Structure

```
context-proxy-mcp/
├── context_proxy_mcp/
│   ├── __init__.py          # Package init
│   ├── server.py            # MCP server, registers all tools
│   ├── memory_store.py      # Local search engine (Chroma vector database)
│   ├── query_log.py         # Query log – for duplicate detection & cache control
│   ├── deepseek_client.py   # DeepSeek API wrapper – compression, storage, retrieval
│   └── config.py            # Configuration
├── pyproject.toml
└── README.md
```

---

## Configuration

All configuration can be done via environment variables:

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | — | OpenRouter API key (primary) |
| `DEEPSEEK_API_KEY` | — | Direct DeepSeek API key (fallback) |
| `CONTEXT_PROXY_MEMORY_DIR` | `.context_proxy_memory` | ChromaDB storage directory |
| `CONTEXT_PROXY_COLLECTION` | `long_term_memory` | ChromaDB collection name |
| `CONTEXT_PROXY_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Embedding model for semantic search |
| `CONTEXT_PROXY_TOP_K` | `5` | Default number of search results |
| `CONTEXT_PROXY_CACHE_TTL` | `300` | Cache TTL in seconds |
| `CONTEXT_PROXY_AGENT_ID` | — | Agent identifier for multi-agent mode |
| `CONTEXT_PROXY_AGENT_NAME` | — | Agent display name |

---

## Contributing

This is an opinionated project born from real multi-agent cost problems.
Feedback, issues, and PRs are very welcome — especially around:

- Integration with other agent frameworks (LangChain, AutoGen, etc.)
- Improved local search / embedding strategies
- More fine-grained cache control strategies
- Real-world benchmarking

---

## License

MIT

---

> *"Don't let your best model remember. Let it think."*
