"""Query logging for duplicate detection and cache control."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import QueryLogConfig, get_config

logger = logging.getLogger(__name__)


@dataclass
class QueryRecord:
    """A record of a single query for cache control."""

    query_hash: str = ""
    query_text: str = ""
    tool_name: str = ""
    agent_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    response_hash: str = ""  # Hash of the response for staleness detection
    result: Optional[dict[str, Any]] = None
    hit_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_hash": self.query_hash,
            "query_text": self.query_text,
            "tool_name": self.tool_name,
            "agent_id": self.agent_id,
            "timestamp": self.timestamp,
            "response_hash": self.response_hash,
            "hit_count": self.hit_count,
        }


class QueryLog:
    """Manages query logging for duplicate detection and cache control.

    Tracks all queries to:
    - Detect duplicate/near-duplicate queries
    - Implement TTL-based cache invalidation
    - Provide cache headers for OpenRouter's free response caching
    - Prevent redundant API calls
    """

    def __init__(self, config: Optional[QueryLogConfig] = None):
        self._config = config or get_config().query_log
        self._records: list[QueryRecord] = []
        self._hash_index: dict[str, int] = {}  # query_hash -> index in _records
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Load query log from disk if not already loaded."""
        if self._loaded:
            return

        log_path = self._config.log_file
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for record_data in data:
                    record = QueryRecord(**record_data)
                    self._records.append(record)
                    self._hash_index[record.query_hash] = len(self._records) - 1
                logger.info(
                    "Loaded %d query log entries from %s",
                    len(self._records),
                    log_path,
                )
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("Failed to load query log: %s", e)
        self._loaded = True

    def _save(self) -> None:
        """Persist query log to disk."""
        try:
            log_path = self._config.log_file
            data = [r.to_dict() for r in self._records]
            # Keep only the most recent entries
            if len(data) > self._config.max_log_entries:
                data = data[-self._config.max_log_entries :]
            with open(log_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except IOError as e:
            logger.warning("Failed to save query log: %s", e)

    @staticmethod
    def _compute_hash(text: str, tool_name: str = "", agent_id: str = "") -> str:
        """Compute a deterministic hash for a query."""
        canonical = f"{tool_name}:{agent_id}:{text.strip().lower()}"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _compute_response_hash(response: Any) -> str:
        """Compute a hash of a response for staleness detection."""
        serialized = json.dumps(response, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    def check_cache(
        self,
        query: str,
        tool_name: str = "",
        agent_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Check if a query has a valid cached result.

        Args:
            query: The query text.
            tool_name: The name of the tool being called.
            agent_id: Optional agent identifier.

        Returns:
            The cached result if valid (within TTL), None otherwise.
        """
        self._ensure_loaded()

        query_hash = self._compute_hash(query, tool_name, agent_id or "")
        idx = self._hash_index.get(query_hash)

        if idx is None:
            return None

        record = self._records[idx]
        age = time.time() - record.timestamp

        if age > self._config.cache_ttl_seconds:
            logger.debug(
                "Cache expired for query '%s' (age=%.1fs, ttl=%ds)",
                query[:50],
                age,
                self._config.cache_ttl_seconds,
            )
            return None

        record.hit_count += 1
        logger.info(
            "Cache HIT for query '%s' (hits=%d, age=%.1fs)",
            query[:50],
            record.hit_count,
            age,
        )
        return record.result

    def record_query(
        self,
        query: str,
        tool_name: str = "",
        agent_id: Optional[str] = None,
        result: Optional[dict[str, Any]] = None,
    ) -> QueryRecord:
        """Record a query and its result.

        Args:
            query: The query text.
            tool_name: The name of the tool being called.
            agent_id: Optional agent identifier.
            result: The result to cache.

        Returns:
            The created/updated QueryRecord.
        """
        self._ensure_loaded()

        query_hash = self._compute_hash(query, tool_name, agent_id or "")
        response_hash = self._compute_response_hash(result) if result else ""

        record = QueryRecord(
            query_hash=query_hash,
            query_text=query[:500],  # Truncate for storage
            tool_name=tool_name,
            agent_id=agent_id,
            timestamp=time.time(),
            response_hash=response_hash,
            result=result,
        )

        # Update existing or append new
        if query_hash in self._hash_index:
            idx = self._hash_index[query_hash]
            old_record = self._records[idx]
            record.hit_count = old_record.hit_count + 1
            self._records[idx] = record
            logger.info(
                "Updated query record for '%s' (total hits=%d)",
                query[:50],
                record.hit_count,
            )
        else:
            self._records.append(record)
            self._hash_index[query_hash] = len(self._records) - 1
            logger.info("Recorded new query: '%s'", query[:50])

        # Trim if needed
        if len(self._records) > self._config.max_log_entries:
            removed = self._records[: len(self._records) - self._config.max_log_entries]
            self._records = self._records[-self._config.max_log_entries :]
            # Rebuild index
            self._hash_index = {
                r.query_hash: i for i, r in enumerate(self._records)
            }
            logger.info("Trimmed %d old query records.", len(removed))

        # Persist (debounced - could be optimized with a timer)
        self._save()

        return record

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about the query log."""
        self._ensure_loaded()

        if not self._records:
            return {
                "total_queries": 0,
                "unique_queries": 0,
                "cache_hits": 0,
                "tools_used": {},
            }

        total_hits = sum(r.hit_count for r in self._records)
        tools: dict[str, int] = {}
        for r in self._records:
            tools[r.tool_name] = tools.get(r.tool_name, 0) + 1

        return {
            "total_queries": len(self._records),
            "unique_queries": len(self._hash_index),
            "cache_hits": total_hits - len(self._records),
            "tools_used": tools,
        }

    def invalidate(self, query: str, tool_name: str = "", agent_id: Optional[str] = None) -> bool:
        """Invalidate a specific cached query.

        Args:
            query: The query text to invalidate.
            tool_name: The tool name.
            agent_id: Optional agent identifier.

        Returns:
            True if an entry was invalidated, False otherwise.
        """
        self._ensure_loaded()

        query_hash = self._compute_hash(query, tool_name, agent_id or "")
        if query_hash in self._hash_index:
            idx = self._hash_index[query_hash]
            self._records.pop(idx)
            # Rebuild index
            self._hash_index = {
                r.query_hash: i for i, r in enumerate(self._records)
            }
            self._save()
            logger.info("Invalidated cache for query '%s'", query[:50])
            return True
        return False

    def clear(self) -> None:
        """Clear all query log entries."""
        self._records.clear()
        self._hash_index.clear()
        self._save()
        logger.info("Cleared all query log entries.")
