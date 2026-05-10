"""DeepSeek API client for compression, storage, and retrieval.

DeepSeek serves as the dedicated "memory processing unit":
- Compresses verbose chat history into concise summaries
- Stores full conversation history in its 1M token context window
- Retrieves relevant information from complete history via deep search
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .config import DeepSeekConfig, get_config

logger = logging.getLogger(__name__)

# System prompts for different operations
COMPRESS_SYSTEM_PROMPT = """\
You are a memory compression specialist. Your job is to take raw conversation \
content and produce a concise, information-dense summary that preserves all \
key facts, decisions, preferences, and context needed for future reference.

Rules:
- Preserve ALL factual information, names, numbers, dates, decisions
- Remove filler words, pleasantries, and redundant explanations
- Use structured format (bullet points for lists, key-value for facts)
- Keep the summary under 200 words unless the content is exceptionally dense
- If the content contains code, preserve code snippets exactly
- Mark any uncertain information with [?]
- Include the original timestamp context if available"""

RECALL_SYSTEM_PROMPT = """\
You are a memory retrieval specialist. Given a conversation history and a query, \
find and extract all relevant information. Be thorough - check for direct matches, \
semantic relevance, and implied connections.

Rules:
- Return ONLY the relevant information, nothing else
- If multiple relevant passages exist, include all of them
- Cite the approximate position in the conversation (beginning/middle/end)
- If nothing relevant is found, say "NO_RELEVANT_INFO"
- Be concise but complete - don't summarize away important details"""

SUMMARIZE_WORKFLOW_SYSTEM_PROMPT = """\
You are a workflow summarization specialist. Given a complete conversation or \
workflow session, extract the key outcomes, decisions, and facts that should be \
permanently remembered.

Rules:
- Extract 3-7 key points maximum
- Each point should be a standalone fact or decision
- Include any action items or pending tasks
- Include any preferences or constraints mentioned
- Format as a numbered list
- If there are code artifacts, mention their purpose and location
- Do NOT include conversational metadata (who said what, greetings, etc.)"""


@dataclass
class ConversationTurn:
    """A single turn in the conversation history."""

    role: str  # "user", "assistant", or "system"
    content: str
    timestamp: float = field(default_factory=time.time)


class DeepSeekClient:
    """Client for DeepSeek API handling compression, storage, and retrieval.

    Manages the full conversation history within DeepSeek's context window
    and provides compression and deep retrieval capabilities.
    """

    def __init__(self, config: Optional[DeepSeekConfig] = None):
        self._config = config or get_config().deepseek
        self._history: list[ConversationTurn] = []
        self._client: Optional[httpx.Client] = None

    def _get_client(self) -> httpx.Client:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            headers = {
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            }
            # Add OpenRouter-specific headers
            if "openrouter" in self._config.base_url:
                headers["HTTP-Referer"] = "https://github.com/context-proxy-mcp"
                headers["X-Title"] = "Context Proxy MCP"
            self._client = httpx.Client(
                base_url=self._config.base_url,
                headers=headers,
                timeout=60.0,
            )
        return self._client

    def _chat(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Send a chat completion request to DeepSeek.

        Args:
            messages: The conversation messages.
            system_prompt: Optional system prompt override.
            temperature: Optional temperature override.
            max_tokens: Optional max tokens override.

        Returns:
            The assistant's response text.
        """
        client = self._get_client()

        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": full_messages,
            "temperature": temperature or self._config.temperature,
            "max_tokens": max_tokens or self._config.max_output_tokens,
        }

        try:
            response = client.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            logger.error(
                "DeepSeek API error: %d %s - %s",
                e.response.status_code,
                e.response.reason_phrase,
                e.response.text[:500],
            )
            raise RuntimeError(
                f"DeepSeek API error: {e.response.status_code} "
                f"{e.response.reason_phrase}"
            ) from e
        except httpx.RequestError as e:
            logger.error("DeepSeek request error: %s", e)
            raise RuntimeError(f"DeepSeek request error: {e}") from e

    def compress(self, content: str) -> str:
        """Compress raw content into a concise summary.

        Args:
            content: The raw content to compress.

        Returns:
            A concise summary preserving key information.
        """
        logger.info("Compressing content (%d chars)", len(content))

        # Add to history before compression
        self._history.append(
            ConversationTurn(role="user", content=content)
        )

        messages = [{"role": "user", "content": f"Compress this:\n\n{content}"}]
        summary = self._chat(messages, system_prompt=COMPRESS_SYSTEM_PROMPT)

        # Add summary to history as well
        self._history.append(
            ConversationTurn(role="assistant", content=summary)
        )

        logger.info(
            "Compressed %d chars -> %d chars (%.1f%% reduction)",
            len(content),
            len(summary),
            (1 - len(summary) / max(len(content), 1)) * 100,
        )
        return summary

    def remember(self, content: str, agent_id: Optional[str] = None) -> dict[str, Any]:
        """Process content for memory storage.

        Compresses the content into a summary, stores the full version
        in the conversation history, and returns both.

        Args:
            content: The raw content to remember.
            agent_id: Optional agent identifier.

        Returns:
            Dict with 'summary', 'original_length', 'summary_length'.
        """
        summary = self.compress(content)
        return {
            "summary": summary,
            "original_length": len(content),
            "summary_length": len(summary),
            "agent_id": agent_id,
        }

    def recall(
        self,
        query: str,
        agent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Deep retrieval from the full conversation history.

        Searches through the complete history stored in DeepSeek's context
        to find relevant information.

        Args:
            query: The search query.
            agent_id: Optional agent identifier (for filtering).

        Returns:
            Dict with 'results', 'history_size', 'tokens_used_approx'.
        """
        if not self._history:
            return {
                "results": "NO_RELEVANT_INFO",
                "history_size": 0,
                "tokens_used_approx": 0,
            }

        logger.info(
            "Recalling from history (%d turns) for query: '%s'",
            len(self._history),
            query[:50],
        )

        # Build conversation context for DeepSeek
        history_text = "\n".join(
            f"[{turn.role}]: {turn.content}"
            for turn in self._history
        )

        messages = [
            {
                "role": "user",
                "content": f"Conversation history:\n{history_text}\n\n"
                f"Query: {query}",
            }
        ]

        result = self._chat(messages, system_prompt=RECALL_SYSTEM_PROMPT)

        # Estimate tokens (rough: 1 token ≈ 4 chars for English)
        approx_tokens = len(history_text) // 4 + len(query) // 4

        return {
            "results": result,
            "history_size": len(self._history),
            "tokens_used_approx": approx_tokens,
        }

    def summarize_workflow(
        self,
        workflow_description: Optional[str] = None,
    ) -> dict[str, Any]:
        """Summarize an entire workflow session for permanent storage.

        Extracts key points from the complete conversation history
        to be stored in the local long-term memory.

        Args:
            workflow_description: Optional description of the workflow type.

        Returns:
            Dict with 'key_points' (list of strings), 'turns_processed'.
        """
        if not self._history:
            return {
                "key_points": [],
                "turns_processed": 0,
            }

        logger.info(
            "Summarizing workflow (%d turns)", len(self._history)
        )

        # Build conversation context
        history_text = "\n".join(
            f"[{turn.role}]: {turn.content}"
            for turn in self._history
        )

        prompt = f"Summarize this workflow session:\n\n{history_text}"
        if workflow_description:
            prompt = f"Workflow type: {workflow_description}\n\n{prompt}"

        messages = [{"role": "user", "content": prompt}]
        result = self._chat(
            messages,
            system_prompt=SUMMARIZE_WORKFLOW_SYSTEM_PROMPT,
            max_tokens=2048,
        )

        # Parse key points from the numbered list
        key_points = [
            line.strip().lstrip("0123456789.-) ")
            for line in result.strip().split("\n")
            if line.strip() and line.strip()[0].isdigit()
        ]
        if not key_points:
            # If parsing fails, use the whole response as a single point
            key_points = [result.strip()]

        return {
            "key_points": key_points,
            "turns_processed": len(self._history),
            "raw_summary": result,
        }

    def add_to_history(
        self,
        role: str,
        content: str,
    ) -> None:
        """Manually add a turn to the conversation history.

        Args:
            role: The role ("user", "assistant", or "system").
            content: The content of the turn.
        """
        self._history.append(
            ConversationTurn(role=role, content=content)
        )

    def clear_history(self) -> int:
        """Clear the conversation history.

        Returns:
            Number of turns cleared.
        """
        count = len(self._history)
        self._history.clear()
        logger.info("Cleared %d turns from history.", count)
        return count

    def get_history_size(self) -> int:
        """Return the number of turns in the conversation history."""
        return len(self._history)

    def get_history_tokens_approx(self) -> int:
        """Estimate the total tokens in the conversation history."""
        total_chars = sum(len(turn.content) for turn in self._history)
        return total_chars // 4  # Rough estimate

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            self._client.close()
            self._client = None
