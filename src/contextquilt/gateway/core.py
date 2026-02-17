"""Core gateway implementation."""

from typing import Any, Dict, List, Optional
import structlog

from src.contextquilt.memory.layer import MemoryLayer
from src.contextquilt.types import ConversationContext, LLMProvider, Message

logger = structlog.get_logger()


class Gateway:
    """
    Main gateway that orchestrates LLM calls with memory enrichment.

    Implements the "Zero-Latency" asynchronous architecture:
    - Synchronous read path: Fast context retrieval
    - Asynchronous write path: Background cognitive consolidation
    """

    def __init__(
        self,
        memory_layer: MemoryLayer,
        llm_provider: LLMProvider,
    ) -> None:
        """Initialize the gateway."""
        self.memory = memory_layer
        self.llm_provider = llm_provider
        self._logger = logger.bind(component="gateway")

    async def complete(
        self,
        messages: List[Message],
        conversation_id: str,
        user_id: str,
        **kwargs: Any,
    ) -> str:
        """
        Generate a completion with memory-enriched context.

        This is the main synchronous read path - must be fast!
        """
        self._logger.info(
            "completion_request",
            conversation_id=conversation_id,
            user_id=user_id,
            message_count=len(messages),
        )

        # Fast context retrieval from working memory
        context = await self.memory.retrieve_context(conversation_id, user_id)

        # Enrich messages with context (in-memory operation, fast)
        enriched_messages = await self._enrich_messages(messages, context)

        # Call LLM provider
        response = await self.llm_provider.complete(enriched_messages, **kwargs)

        self._logger.info(
            "completion_success",
            conversation_id=conversation_id,
        )

        # TODO: Trigger async background processing for memory consolidation
        # This should NOT block the response

        return response

    async def _enrich_messages(
        self,
        messages: List[Message],
        context: ConversationContext,
    ) -> List[Message]:
        """
        Enrich messages with context (synchronous, must be fast).

        TODO: Implement context compression (ACON, LLMLingua-2)
        """
        # For now, just return messages as-is
        # In production, this would inject relevant context from memory
        return messages
