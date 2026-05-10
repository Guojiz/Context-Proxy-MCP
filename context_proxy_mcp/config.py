"""Configuration management for Context Proxy MCP."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DeepSeekConfig:
    """Configuration for the DeepSeek API client."""

    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "deepseek/deepseek-chat-v3-0324"
    max_context_tokens: int = 1_000_000
    temperature: float = 0.3
    max_output_tokens: int = 4096

    def __post_init__(self):
        if not self.api_key:
            self.api_key = os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get(
                "DEEPSEEK_API_KEY", ""
            )
        # If using direct DeepSeek API, adjust base URL
        if os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("OPENROUTER_API_KEY"):
            self.base_url = "https://api.deepseek.com"
            self.model = "deepseek-chat"


@dataclass
class MemoryStoreConfig:
    """Configuration for the local ChromaDB memory store."""

    persist_directory: str = ".context_proxy_memory"
    collection_name: str = "long_term_memory"
    embedding_model: str = "all-MiniLM-L6-v2"
    top_k: int = 5  # Number of results to return from semantic search


@dataclass
class QueryLogConfig:
    """Configuration for query logging and cache control."""

    log_file: str = ".context_proxy_query_log.json"
    cache_ttl_seconds: int = 300  # 5 minutes default TTL for cached results
    max_log_entries: int = 10000


@dataclass
class Config:
    """Top-level configuration for Context Proxy MCP."""

    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    memory_store: MemoryStoreConfig = field(default_factory=MemoryStoreConfig)
    query_log: QueryLogConfig = field(default_factory=QueryLogConfig)

    # Agent identity (for multi-agent scenarios)
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Config":
        """Create configuration from environment variables."""
        return cls(
            deepseek=DeepSeekConfig(),
            memory_store=MemoryStoreConfig(
                persist_directory=os.environ.get(
                    "CONTEXT_PROXY_MEMORY_DIR", ".context_proxy_memory"
                ),
                collection_name=os.environ.get(
                    "CONTEXT_PROXY_COLLECTION", "long_term_memory"
                ),
                embedding_model=os.environ.get(
                    "CONTEXT_PROXY_EMBEDDING_MODEL", "all-MiniLM-L6-v2"
                ),
                top_k=int(os.environ.get("CONTEXT_PROXY_TOP_K", "5")),
            ),
            query_log=QueryLogConfig(
                log_file=os.environ.get(
                    "CONTEXT_PROXY_QUERY_LOG", ".context_proxy_query_log.json"
                ),
                cache_ttl_seconds=int(
                    os.environ.get("CONTEXT_PROXY_CACHE_TTL", "300")
                ),
                max_log_entries=int(
                    os.environ.get("CONTEXT_PROXY_MAX_LOG", "10000")
                ),
            ),
            agent_id=os.environ.get("CONTEXT_PROXY_AGENT_ID"),
            agent_name=os.environ.get("CONTEXT_PROXY_AGENT_NAME"),
        )


# Global config singleton
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global configuration, initializing from env if needed."""
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config


def reset_config() -> None:
    """Reset the global configuration (useful for testing)."""
    global _config
    _config = None
