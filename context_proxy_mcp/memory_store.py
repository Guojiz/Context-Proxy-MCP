"""Local long-term memory store using ChromaDB for semantic search."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import MemoryStoreConfig, get_config

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """A single memory entry stored in the long-term memory."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    content: str = ""
    summary: str = ""
    agent_id: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "summary": self.summary,
            "agent_id": self.agent_id,
            "tags": self.tags,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


class MemoryStore:
    """Local vector-based long-term memory using ChromaDB.

    Provides persistent, searchable storage for key decisions, facts,
    and conclusions across sessions.
    """

    def __init__(self, config: Optional[MemoryStoreConfig] = None):
        self._config = config or get_config().memory_store
        self._client = None
        self._collection = None
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Lazy initialization of ChromaDB client and collection."""
        if self._initialized:
            return

        import chromadb

        logger.info(
            "Initializing ChromaDB at: %s", self._config.persist_directory
        )
        self._client = chromadb.PersistentClient(path=self._config.persist_directory)
        self._collection = self._client.get_or_create_collection(
            name=self._config.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._initialized = True
        logger.info(
            "ChromaDB initialized. Collection '%s' has %d entries.",
            self._config.collection_name,
            self._collection.count(),
        )

    def store(
        self,
        content: str,
        summary: str = "",
        agent_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        """Store a new memory entry.

        Args:
            content: The original content to remember.
            summary: An optional compressed summary of the content.
            agent_id: Optional agent identifier for multi-agent scenarios.
            tags: Optional list of tags for categorization.
            metadata: Optional additional metadata.

        Returns:
            The ID of the stored memory entry.
        """
        self._ensure_initialized()

        entry_id = str(uuid.uuid4())
        entry_metadata: dict[str, Any] = {
            "timestamp": time.time(),
            "agent_id": agent_id or "",
            "tags": ",".join(tags) if tags else "",
        }
        if metadata:
            entry_metadata.update(metadata)

        # Store both the summary (for quick retrieval) and full content
        text_to_embed = summary if summary else content
        self._collection.add(
            documents=[text_to_embed],
            metadatas=[entry_metadata],
            ids=[entry_id],
        )

        # Store full content in metadata if it differs from the embedded text
        if content and content != text_to_embed:
            # ChromaDB metadata values have a size limit; store in a separate
            # mechanism if content is very large
            if len(content) <= 10000:
                self._collection.update(
                    ids=[entry_id],
                    metadatas=[{**entry_metadata, "full_content": content}],
                )

        logger.info("Stored memory entry %s (agent=%s)", entry_id, agent_id)
        return entry_id

    def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        agent_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Search for relevant memories using semantic similarity.

        Args:
            query: The search query.
            top_k: Number of results to return (defaults to config).
            agent_id: If set, filter to memories from this agent.
            tags: If set, filter to memories with any of these tags.

        Returns:
            List of matching memory entries with relevance scores.
        """
        self._ensure_initialized()

        if self._collection.count() == 0:
            return []

        k = top_k or self._config.top_k
        where_filter: Optional[dict[str, Any]] = None

        # Build filter conditions
        conditions = []
        if agent_id:
            conditions.append({"agent_id": agent_id})
        if tags:
            # ChromaDB where filter: match any tag
            tag_conditions = [{"tags": tag} for tag in tags]
            if len(tag_conditions) == 1:
                conditions.append(tag_conditions[0])
            else:
                conditions.append({"$or": tag_conditions})

        if conditions:
            if len(conditions) == 1:
                where_filter = conditions[0]
            else:
                where_filter = {"$and": conditions}

        kwargs: dict[str, Any] = {
            "query_texts": [query],
            "n_results": min(k, self._collection.count()),
        }
        if where_filter:
            kwargs["where"] = where_filter

        results = self._collection.query(**kwargs)

        entries = []
        if results and results["ids"] and results["ids"][0]:
            for i, entry_id in enumerate(results["ids"][0]):
                entry_data: dict[str, Any] = {
                    "id": entry_id,
                    "content": results["documents"][0][i] if results["documents"] else "",
                    "score": results["distances"][0][i] if results["distances"] else 0,
                }
                if results["metadatas"] and results["metadatas"][0]:
                    meta = results["metadatas"][0][i]
                    entry_data["metadata"] = meta
                    # Restore full content if available
                    if "full_content" in meta:
                        entry_data["full_content"] = meta["full_content"]
                    if "agent_id" in meta:
                        entry_data["agent_id"] = meta["agent_id"]
                    if "tags" in meta:
                        entry_data["tags"] = [
                            t.strip() for t in meta["tags"].split(",") if t.strip()
                        ]
                    if "timestamp" in meta:
                        entry_data["timestamp"] = meta["timestamp"]
                entries.append(entry_data)

        logger.info(
            "Search for '%s' returned %d results (agent=%s)",
            query[:50],
            len(entries),
            agent_id,
        )
        return entries

    def get_recent(
        self,
        limit: int = 10,
        agent_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Get the most recently stored memory entries.

        Args:
            limit: Maximum number of entries to return.
            agent_id: If set, filter to memories from this agent.

        Returns:
            List of recent memory entries.
        """
        self._ensure_initialized()

        if self._collection.count() == 0:
            return []

        where_filter: Optional[dict[str, Any]] = None
        if agent_id:
            where_filter = {"agent_id": agent_id}

        kwargs: dict[str, Any] = {
            "limit": min(limit, self._collection.count()),
        }
        if where_filter:
            kwargs["where"] = where_filter

        # Get all entries and sort by timestamp
        results = self._collection.get(**kwargs)

        if not results or not results["ids"]:
            return []

        entries = []
        for i, entry_id in enumerate(results["ids"]):
            entry_data: dict[str, Any] = {
                "id": entry_id,
                "content": results["documents"][i] if results["documents"] else "",
            }
            if results["metadatas"]:
                meta = results["metadatas"][i]
                entry_data["metadata"] = meta
                if "full_content" in meta:
                    entry_data["full_content"] = meta["full_content"]
                if "agent_id" in meta:
                    entry_data["agent_id"] = meta["agent_id"]
                if "tags" in meta:
                    entry_data["tags"] = [
                        t.strip() for t in meta["tags"].split(",") if t.strip()
                    ]
                if "timestamp" in meta:
                    entry_data["timestamp"] = meta["timestamp"]
            entries.append(entry_data)

        # Sort by timestamp descending (most recent first)
        entries.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return entries[:limit]

    def delete(self, entry_id: str) -> bool:
        """Delete a specific memory entry.

        Args:
            entry_id: The ID of the entry to delete.

        Returns:
            True if the entry was deleted, False otherwise.
        """
        self._ensure_initialized()

        try:
            self._collection.delete(ids=[entry_id])
            logger.info("Deleted memory entry %s", entry_id)
            return True
        except Exception as e:
            logger.warning("Failed to delete memory entry %s: %s", entry_id, e)
            return False

    def delete_by_agent(self, agent_id: str) -> int:
        """Delete all memory entries for a specific agent.

        Args:
            agent_id: The agent identifier.

        Returns:
            Number of entries deleted.
        """
        self._ensure_initialized()

        try:
            # First, get all IDs for this agent
            results = self._collection.get(
                where={"agent_id": agent_id},
            )
            if results and results["ids"]:
                self._collection.delete(ids=results["ids"])
                count = len(results["ids"])
                logger.info(
                    "Deleted %d memory entries for agent %s", count, agent_id
                )
                return count
            return 0
        except Exception as e:
            logger.warning(
                "Failed to delete entries for agent %s: %s", agent_id, e
            )
            return 0

    def count(self) -> int:
        """Return the total number of stored memory entries."""
        self._ensure_initialized()
        return self._collection.count()

    def clear(self) -> None:
        """Clear all memory entries. Use with caution."""
        self._ensure_initialized()
        # Delete and recreate the collection
        self._client.delete_collection(self._config.collection_name)
        self._collection = self._client.get_or_create_collection(
            name=self._config.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("Cleared all memory entries.")
