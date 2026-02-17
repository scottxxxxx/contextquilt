"""Core memory layer implementation."""

from typing import Any, Dict, List, Optional
import structlog

from ..types import ConversationContext, ContextPatch, MemoryStore, MemoryType, PatchCategory, PatchPersistence, PatchOriginMode, PatchSource

logger = structlog.get_logger()


class MemoryLayer:
    """
    Central memory orchestration layer.

    Implements the "Hybrid Cognitive" data model with:
    - Factual memory (K/V store)
    - Episodic memory (Graph database)
    - Working memory (Redis cache)
    """

    def __init__(
        self,
        factual_store: MemoryStore,
        episodic_store: MemoryStore,
        working_store: MemoryStore,
    ) -> None:
        """Initialize the memory layer with three store implementations."""
        self.factual_store = factual_store
        self.episodic_store = episodic_store
        self.working_store = working_store
        self._logger = logger.bind(component="memory_layer")

    async def retrieve_context(
        self, conversation_id: str, user_id: str
    ) -> ConversationContext:
        """
        Retrieve context for a conversation (synchronous read path).

        This is the fast path - queries working memory cache with minimal latency.
        """
        self._logger.info(
            "retrieving_context",
            conversation_id=conversation_id,
            user_id=user_id,
        )

        # Query working memory first (fastest)
        working_key = f"context:{conversation_id}"
        cached_entry = await self.working_store.get(working_key)

        if cached_entry:
            self._logger.debug("cache_hit", key=working_key)
            # Type narrowing - we know the value should be a dict
            if isinstance(cached_entry.value, dict):
                return ConversationContext(**cached_entry.value)

        # Cache miss - build context from other stores
        self._logger.debug("cache_miss", key=working_key)
        context = ConversationContext(
            conversation_id=conversation_id,
            user_id=user_id,
        )

        # Store in working memory for next time
        await self._cache_context(context)

        return context

    async def store_memory(
        self,
        patch_type: PatchCategory,
        patch_name: str,
        value: Any,
        subject_key: str,
        metadata: Optional[Dict[str, Any]] = None,
        ttl: Optional[int] = None,
        persistence: PatchPersistence = PatchPersistence.STICKY,
        origin_mode: PatchOriginMode = PatchOriginMode.INFERRED,
    ) -> None:
        """
        Store a memory entry (can be used in async write path).
        """
        patch = ContextPatch(
            patch_type=patch_type,
            patch_name=patch_name,
            value=value,
            subject_key=subject_key,
            metadata=metadata or {},
            ttl=ttl,
            persistence=persistence,
            origin_mode=origin_mode,
        )

        store = self._get_store(patch.memory_type)
        await store.set(patch)

        self._logger.info(
            "memory_stored",
            memory_type=patch.memory_type,
            patch_name=patch_name,
        )

    async def _cache_context(self, context: ConversationContext) -> None:
        """Cache context in working memory."""
        cache_key = f"context:{context.conversation_id}"
        patch = ContextPatch(
            patch_type=PatchCategory.CONTEXT,
            patch_name=cache_key,
            subject_key=f"user:{context.user_id}",
            value=context.model_dump(),
            ttl=3600,  # 1 hour TTL for working memory
            persistence=PatchPersistence.EPHEMERAL,
            origin_mode=PatchOriginMode.DERIVED,
        )
        await self.working_store.set(patch)

    def _get_store(self, memory_type: MemoryType) -> MemoryStore:
        """Get the appropriate store for a memory type."""
        if memory_type == MemoryType.FACTUAL:
            return self.factual_store
        elif memory_type == MemoryType.EPISODIC:
            return self.episodic_store
        elif memory_type == MemoryType.WORKING:
            return self.working_store
        else:
            raise ValueError(f"Unknown memory type: {memory_type}")
