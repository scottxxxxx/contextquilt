"""Core type definitions for ContextQuilt."""

from enum import Enum
from datetime import datetime, timezone
import uuid
from typing import Any, Dict, List, Optional, Protocol
from pydantic import BaseModel, Field




# Missing Type Definitions

class Message(BaseModel):
    """A chat message."""
    role: str
    content: str


class ConversationContext(BaseModel):
    """Context for a conversation."""
    conversation_id: str
    user_id: str
    variables: Dict[str, Any] = Field(default_factory=dict)


class MemoryType(str, Enum):
    """Types of memory stores."""
    FACTUAL = "factual"
    EPISODIC = "episodic"
    WORKING = "working"


class PatchPersistence(str, Enum):
    """Persistence level of a patch."""
    PERMANENT = "permanent"
    STICKY = "sticky"
    EPHEMERAL = "ephemeral"
    DECAYING = "decaying"


class PatchSource(str, Enum):
    """Source of a patch."""
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    EXTERNAL = "external"
    SYSTEM = "system"


class PatchCategory(str, Enum):
    """
    Category of the Context Patch.
    Valid categories: identity, preference, trait, experience.
    """
    IDENTITY = "identity"
    PREFERENCE = "preference"
    TRAIT = "trait"
    EXPERIENCE = "experience"


class PatchOriginMode(str, Enum):
    """How the patch was created."""
    DECLARED = "declared"
    INFERRED = "inferred"
    DERIVED = "derived"


class PatchSourcePrompt(str, Enum):
    """Which prompt created this patch."""
    NONE = "none"
    DETECTIVE = "detective"
    TAILOR = "tailor"
    ANALYST = "analyst"
    ARCHIVIST = "archivist"
    MANUAL = "manual"
    OTHER = "other"


class PatchSensitivity(str, Enum):
    """Sensitivity level for governance."""
    NORMAL = "normal"
    PII = "pii"
    PHI = "phi"
    SECRET = "secret"


class PatchValueType(str, Enum):
    """Hint for value type."""
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"


class ContextPatch(BaseModel):
    """
    A 'Context Patch' - A discrete unit of user context.
    First-class entity with identity and ownership.
    """

    # Infra / Identity
    patch_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Global unique ID (UUID)")
    # subject_key removed in favor of separate association table for many-to-many

    # Semantics
    patch_name: str = Field(..., description="Semantic name (e.g., 'food_allergies')")
    patch_type: PatchCategory = Field(..., description="Semantic category of the patch")
    value: Any = Field(..., description="The content of the patch (JsonValue)")
    origin_mode: PatchOriginMode = Field(..., description="How this was created")
    source_prompt: PatchSourcePrompt = Field(default=PatchSourcePrompt.NONE, description="Which prompt created this")
    confidence: float = Field(..., Ge=0.0, Le=1.0, description="Confidence score (0.0 - 1.0)")

    # Governance
    sensitivity: Optional[PatchSensitivity] = Field(default=PatchSensitivity.NORMAL, description="Data sensitivity classification")
    value_type_hint: Optional[PatchValueType] = Field(None, description="Hint for value type")
    
    # Lifecycle
    created_at: Optional[str] = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(), description="Creation timestamp (ISO 8601)")
    updated_at: Optional[str] = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(), description="Last update timestamp (ISO 8601)")

    # Lineage
    source_patch_ids: Optional[List[str]] = Field(default=None, description="IDs of patches used to derive this one")


class PatchAccessControl(BaseModel):
    """
    Access Control Layer for a Context Patch.
    Controls which apps can read/write/delete a specific patch.
    """
    patch_id: str = Field(..., description="The patch being controlled")
    app_id: str = Field(..., description="The application requesting access")
    can_read: bool = Field(default=True, description="Can this app read the patch?")
    can_write: bool = Field(default=False, description="Can this app update the patch?")
    can_delete: bool = Field(default=False, description="Can this app delete the patch?")


class LLMProvider(Protocol):
    """Protocol for LLM provider implementations."""

    async def complete(
        self, messages: List[Message], **kwargs: Any
    ) -> str:
        """Generate a completion for the given messages."""
        ...


class MemoryStore(Protocol):
    """Protocol for memory store implementations."""

    async def get(self, key: str) -> Optional[ContextPatch]:
        """Retrieve a memory entry."""
        ...

    async def set(self, entry: ContextPatch) -> None:
        """Store a memory entry."""
        ...

    async def delete(self, key: str) -> None:
        """Delete a memory entry."""
        ...
