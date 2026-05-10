"""Context Proxy MCP Server.

Registers all MCP tools for context management:
- remember: Submit content for compression; DeepSeek returns summary and stores original.
- recall: Search local long-term memory, fall back to DeepSeek full history if needed.
- catch: Retrieve recent key memories (fast context recovery).
- forget: Delete specific memory entries.
- summarize_workflow: End-of-workflow call to distill session into permanent memory.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .config import get_config, reset_config
from .deepseek_client import DeepSeekClient
from .memory_store import MemoryStore
from .query_log import QueryLog

logger = logging.getLogger(__name__)

# Create the MCP server
mcp = FastMCP(
    "context-proxy",
    instructions=(
        "Context Proxy MCP - Offload context management to cheap models. "
        "Use 'remember' to store information, 'recall' to search memories, "
        "'catch' to quickly recover recent context, 'forget' to delete memories, "
        "and 'summarize_workflow' at the end of a workflow to save key insights."
    ),
)

# Global instances (initialized lazily)
_memory_store: Optional[MemoryStore] = None
_deepseek_client: Optional[DeepSeekClient] = None
_query_log: Optional[QueryLog] = None


def _get_memory_store() -> MemoryStore:
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
    return _memory_store


def _get_deepseek_client() -> DeepSeekClient:
    global _deepseek_client
    if _deepseek_client is None:
        _deepseek_client = DeepSeekClient()
    return _deepseek_client


def _get_query_log() -> QueryLog:
    global _query_log
    if _query_log is None:
        _query_log = QueryLog()
    return _query_log


def _get_agent_id(kwargs: dict[str, Any]) -> Optional[str]:
    """Extract agent_id from tool arguments if provided."""
    return kwargs.get("agent_id")


# ─── Tool: remember ───────────────────────────────────────────────────────────


@mcp.tool()
async def remember(
    content: str,
    tags: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> str:
    """Submit content for compression. DeepSeek returns a concise summary and
    stores the original content in its full history. The summary is also saved
    to local long-term memory for permanent retention.

    Args:
        content: The raw content to remember (conversation turns, facts, decisions, etc.)
        tags: Optional comma-separated tags for categorization (e.g. "decision,architecture")
        agent_id: Optional agent identifier for multi-agent scenarios
    """
    config = get_config()
    effective_agent_id = agent_id or config.agent_id

    # Check cache first
    query_log = _get_query_log()
    cached = query_log.check_cache(content, "remember", effective_agent_id)
    if cached:
        return json.dumps(
            {
                "status": "cached",
                "summary": cached.get("summary", ""),
                "original_length": cached.get("original_length", 0),
                "summary_length": cached.get("summary_length", 0),
                "message": "Returned cached compressed result.",
            },
            ensure_ascii=False,
            indent=2,
        )

    # Compress via DeepSeek
    deepseek = _get_deepseek_client()
    result = deepseek.remember(content, agent_id=effective_agent_id)

    # Store in local long-term memory
    memory = _get_memory_store()
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    memory.store(
        content=content,
        summary=result["summary"],
        agent_id=effective_agent_id,
        tags=tag_list,
        metadata={"tool": "remember", "original_length": result["original_length"]},
    )

    # Record in query log
    query_log.record_query(
        query=content,
        tool_name="remember",
        agent_id=effective_agent_id,
        result=result,
    )

    return json.dumps(
        {
            "status": "ok",
            "summary": result["summary"],
            "original_length": result["original_length"],
            "summary_length": result["summary_length"],
            "compression_ratio": round(
                result["summary_length"] / max(result["original_length"], 1) * 100, 1
            ),
            "message": "Content compressed and stored. Summary saved to long-term memory.",
        },
        ensure_ascii=False,
        indent=2,
    )


# ─── Tool: recall ─────────────────────────────────────────────────────────────


@mcp.tool()
async def recall(
    query: str,
    deep_search: bool = False,
    top_k: Optional[int] = None,
    agent_id: Optional[str] = None,
) -> str:
    """Search local long-term memory for relevant information. If 'deep_search'
    is enabled and local results are insufficient, falls back to DeepSeek's
    complete conversation history for thorough retrieval.

    Args:
        query: What to search for (natural language question or keyword)
        deep_search: If true, also search DeepSeek's full history when local results are insufficient
        top_k: Maximum number of results to return (default: 5)
        agent_id: Optional agent identifier for multi-agent scenarios
    """
    config = get_config()
    effective_agent_id = agent_id or config.agent_id

    # Check cache first
    query_log = _get_query_log()
    cached = query_log.check_cache(query, "recall", effective_agent_id)
    if cached:
        return json.dumps(
            {
                "status": "cached",
                "results": cached.get("results", []),
                "message": "Returned cached recall results.",
            },
            ensure_ascii=False,
            indent=2,
        )

    # Search local long-term memory first
    memory = _get_memory_store()
    local_results = memory.search(
        query=query,
        top_k=top_k,
        agent_id=effective_agent_id,
    )

    results: list[dict[str, Any]] = []
    deep_results = None

    for r in local_results:
        entry = {
            "id": r["id"],
            "content": r.get("full_content", r["content"]),
            "summary": r["content"],
            "score": round(1 - r["score"], 4),  # Convert distance to similarity
            "source": "local_memory",
        }
        if r.get("timestamp"):
            entry["timestamp"] = r["timestamp"]
        if r.get("tags"):
            entry["tags"] = r["tags"]
        results.append(entry)

    # Deep search fallback if enabled
    if deep_search:
        deepseek = _get_deepseek_client()
        deep_result = deepseek.recall(query, agent_id=effective_agent_id)

        if deep_result["results"] != "NO_RELEVANT_INFO":
            deep_results = {
                "content": deep_result["results"],
                "source": "deepseek_history",
                "history_size": deep_result["history_size"],
                "tokens_used_approx": deep_result["tokens_used_approx"],
            }

    # Record in query log
    query_log.record_query(
        query=query,
        tool_name="recall",
        agent_id=effective_agent_id,
        result={
            "local_count": len(results),
            "has_deep_results": deep_results is not None,
        },
    )

    response: dict[str, Any] = {
        "status": "ok",
        "local_results": results,
        "local_results_count": len(results),
    }
    if deep_results:
        response["deep_search_results"] = deep_results
    response["message"] = (
        f"Found {len(results)} local results."
        + (" Deep search also returned results." if deep_results else "")
    )

    return json.dumps(response, ensure_ascii=False, indent=2)


# ─── Tool: catch ──────────────────────────────────────────────────────────────


@mcp.tool()
async def catch(
    limit: int = 10,
    agent_id: Optional[str] = None,
) -> str:
    """Retrieve the most recent key memories for fast context recovery.
    Use this at the start of a new session to quickly restore context
    without replaying full history.

    Args:
        limit: Maximum number of recent memories to retrieve (default: 10)
        agent_id: Optional agent identifier for multi-agent scenarios
    """
    config = get_config()
    effective_agent_id = agent_id or config.agent_id

    memory = _get_memory_store()
    recent = memory.get_recent(limit=limit, agent_id=effective_agent_id)

    entries = []
    for r in recent:
        entry = {
            "id": r["id"],
            "content": r.get("full_content", r["content"]),
            "summary": r["content"],
            "source": "local_memory",
        }
        if r.get("timestamp"):
            entry["timestamp"] = r["timestamp"]
        if r.get("tags"):
            entry["tags"] = r["tags"]
        entries.append(entry)

    return json.dumps(
        {
            "status": "ok",
            "recent_memories": entries,
            "count": len(entries),
            "total_stored": memory.count(),
            "message": (
                f"Retrieved {len(entries)} recent memories. "
                f"Total stored: {memory.count()}."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


# ─── Tool: forget ─────────────────────────────────────────────────────────────


@mcp.tool()
async def forget(
    memory_id: str,
    agent_id: Optional[str] = None,
) -> str:
    """Delete a specific memory entry from long-term storage.
    Use this to remove outdated, incorrect, or sensitive information.

    Args:
        memory_id: The ID of the memory entry to delete (obtained from recall/catch results)
        agent_id: Optional agent identifier for multi-agent scenarios
    """
    config = get_config()
    effective_agent_id = agent_id or config.agent_id

    memory = _get_memory_store()
    success = memory.delete(memory_id)

    # Invalidate any cached queries that might reference this memory
    if success:
        query_log = _get_query_log()
        query_log.clear()  # Simple approach: clear cache on deletion

    return json.dumps(
        {
            "status": "deleted" if success else "not_found",
            "memory_id": memory_id,
            "message": (
                f"Memory {memory_id} deleted successfully."
                if success
                else f"Memory {memory_id} not found."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


# ─── Tool: summarize_workflow ─────────────────────────────────────────────────


@mcp.tool()
async def summarize_workflow(
    workflow_type: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> str:
    """End-of-workflow call. DeepSeek distills the entire session into
    key points, which are then permanently stored in local long-term memory.
    Call this when a workflow or task session is complete.

    Args:
        workflow_type: Optional description of the workflow (e.g. "code_review", "planning")
        agent_id: Optional agent identifier for multi-agent scenarios
    """
    config = get_config()
    effective_agent_id = agent_id or config.agent_id

    # Get summary from DeepSeek
    deepseek = _get_deepseek_client()
    result = deepseek.summarize_workflow(workflow_description=workflow_type)

    if not result["key_points"]:
        return json.dumps(
            {
                "status": "no_content",
                "message": "No conversation history to summarize.",
            },
            ensure_ascii=False,
            indent=2,
        )

    # Store each key point in local long-term memory
    memory = _get_memory_store()
    stored_ids = []
    for point in result["key_points"]:
        memory_id = memory.store(
            content=point,
            summary=point,
            agent_id=effective_agent_id,
            tags=["workflow_summary", workflow_type or "general"]
            if workflow_type
            else ["workflow_summary"],
            metadata={
                "tool": "summarize_workflow",
                "workflow_type": workflow_type or "general",
                "turns_processed": result["turns_processed"],
            },
        )
        stored_ids.append(memory_id)

    # Clear DeepSeek history after successful summarization
    turns_cleared = deepseek.clear_history()

    return json.dumps(
        {
            "status": "ok",
            "key_points": result["key_points"],
            "points_count": len(result["key_points"]),
            "stored_ids": stored_ids,
            "turns_processed": result["turns_processed"],
            "history_cleared": turns_cleared,
            "message": (
                f"Extracted {len(result['key_points'])} key points and stored them "
                f"in long-term memory. Cleared {turns_cleared} turns from working history."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


# ─── Server entry point ───────────────────────────────────────────────────────


def main():
    """Run the Context Proxy MCP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = get_config()
    logger.info("Starting Context Proxy MCP server...")
    logger.info("DeepSeek model: %s", config.deepseek.model)
    logger.info("DeepSeek base URL: %s", config.deepseek.base_url)
    logger.info(
        "API key configured: %s",
        "yes" if config.deepseek.api_key else "NO - set OPENROUTER_API_KEY or DEEPSEEK_API_KEY",
    )

    if not config.deepseek.api_key:
        logger.warning(
            "No API key configured! Set OPENROUTER_API_KEY or DEEPSEEK_API_KEY environment variable."
        )

    # Run the MCP server
    mcp.run()


if __name__ == "__main__":
    main()
