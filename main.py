"""
Context Quilt - Main Application Entry Point
"""

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import os
from datetime import datetime
import asyncio
from openai import AsyncOpenAI

# Initialize FastAPI app
app = FastAPI(
    title="Context Quilt",
    description="LLM Gateway with Unified Memory & Context Enrichment",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize OpenAI client
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ============================================
# Request/Response Models
# ============================================

class Message(BaseModel):
    """Chat message model"""
    role: str = Field(..., description="Role: user, assistant, or system")
    content: str = Field(..., description="Message content")


class ContextEnrichment(BaseModel):
    """Context enrichment configuration"""
    enabled: bool = Field(default=True, description="Enable context enrichment")
    include_history: bool = Field(default=True, description="Include conversation history")
    max_history_messages: int = Field(default=5, description="Maximum history messages to include")


class ChatRequest(BaseModel):
    """Chat completion request"""
    user_id: str = Field(..., description="Unique user identifier")
    session_id: Optional[str] = Field(None, description="Session identifier for grouping conversations")
    application: str = Field(..., description="Application name (e.g., support_bot, sales_assistant)")
    messages: List[Message] = Field(..., description="List of chat messages")
    model: Optional[str] = Field(default="gpt-4", description="LLM model to use")
    temperature: Optional[float] = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=1000, gt=0)
    context_enrichment: Optional[ContextEnrichment] = Field(default_factory=ContextEnrichment)
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional metadata")


class ContextQuiltMetadata(BaseModel):
    """Context Quilt processing metadata"""
    patches_used: List[str] = Field(default_factory=list, description="Context patches applied")
    enrichment_tokens: int = Field(default=0, description="Tokens added by enrichment")
    cached: bool = Field(default=False, description="Whether response was cached")


class ChatResponse(BaseModel):
    """Chat completion response"""
    id: str = Field(..., description="Unique request ID")
    model_used: str = Field(..., description="Model that generated the response")
    context_quilt: ContextQuiltMetadata = Field(..., description="Context Quilt metadata")
    response: Message = Field(..., description="Assistant response message")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Response metadata")


class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    version: str
    timestamp: str


class MemoryResponse(BaseModel):
    """User memory response"""
    user_id: str
    profile: Dict[str, Any]
    history_count: int
    last_interaction: Optional[str]


# ============================================
# Authentication
# ============================================

async def verify_api_key(authorization: str = Header(...)) -> str:
    """
    Verify API key from Authorization header
    Format: Bearer {api_key}
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    
    api_key = authorization.replace("Bearer ", "")
    
    # TODO: Implement proper API key validation against database
    # For MVP, just check if key is present
    if not api_key or len(api_key) < 10:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    return api_key


# ============================================
# In-Memory Storage (MVP - Replace with DB)
# ============================================

# Simple in-memory storage for MVP
# In production, replace with PostgreSQL + Redis
user_memories: Dict[str, Dict[str, Any]] = {}
conversation_history: Dict[str, List[Dict[str, Any]]] = {}


def get_user_memory(user_id: str) -> Dict[str, Any]:
    """Retrieve user memory from storage"""
    if user_id not in user_memories:
        user_memories[user_id] = {
            "profile": {},
            "preferences": {},
            "context": {}
        }
    return user_memories[user_id]


def get_conversation_history(user_id: str, max_messages: int = 5) -> List[Dict[str, Any]]:
    """Retrieve recent conversation history"""
    history = conversation_history.get(user_id, [])
    return history[-max_messages:] if history else []


def save_interaction(user_id: str, messages: List[Message], response: str, metadata: Dict[str, Any]):
    """Save interaction to history"""
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    
    conversation_history[user_id].append({
        "timestamp": datetime.utcnow().isoformat(),
        "messages": [msg.dict() for msg in messages],
        "response": response,
        "metadata": metadata
    })


def enrich_prompt(
    user_id: str,
    application: str,
    messages: List[Message],
    enrichment_config: ContextEnrichment
) -> tuple[List[Message], ContextQuiltMetadata]:
    """
    Enrich the prompt with context from user memory and history
    Returns: (enriched_messages, metadata)
    """
    patches_used = []
    enrichment_tokens = 0
    
    # Get user memory
    memory = get_user_memory(user_id)
    
    # Build context based on application type
    context_parts = []
    
    # Add role-based context
    role_contexts = {
        "support_bot": "You are a helpful customer support assistant. Be empathetic and solution-oriented.",
        "sales_assistant": "You are a consultative sales assistant. Identify needs and suggest appropriate solutions.",
        "internal_tool": "You are an internal assistant for employees. Assume technical knowledge and be concise.",
    }
    
    if application in role_contexts:
        context_parts.append(f"[ROLE CONTEXT]\n{role_contexts[application]}")
        patches_used.append("role_context")
    
    # Add user profile if available
    if memory.get("profile"):
        context_parts.append(f"[USER PROFILE]\n{memory['profile']}")
        patches_used.append("user_profile")
    
    # Add conversation history if enabled
    if enrichment_config.include_history:
        history = get_conversation_history(user_id, enrichment_config.max_history_messages)
        if history:
            history_summary = "\n".join([
                f"- {h['timestamp']}: {h['metadata'].get('summary', 'Conversation')}"
                for h in history[-3:]  # Last 3 interactions
            ])
            context_parts.append(f"[RECENT HISTORY]\n{history_summary}")
            patches_used.append("conversation_history")
    
    # Combine context
    if context_parts:
        enriched_context = "\n\n".join(context_parts)
        # Estimate tokens (rough estimate: 1 token ≈ 4 characters)
        enrichment_tokens = len(enriched_context) // 4
        
        # Prepend context as system message
        system_message = Message(
            role="system",
            content=enriched_context
        )
        enriched_messages = [system_message] + messages
    else:
        enriched_messages = messages
    
    metadata = ContextQuiltMetadata(
        patches_used=patches_used,
        enrichment_tokens=enrichment_tokens,
        cached=False
    )
    
    return enriched_messages, metadata


# ============================================
# API Endpoints
# ============================================

@app.get("/", tags=["Root"])
async def root():
    """Root endpoint"""
    return {
        "service": "Context Quilt",
        "version": "1.0.0",
        "status": "operational",
        "docs": "/docs"
    }


@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check():
    """Health check endpoint"""
    return HealthResponse(
        status="healthy",
        version="1.0.0",
        timestamp=datetime.utcnow().isoformat()
    )


@app.post("/v1/chat", response_model=ChatResponse, tags=["Chat"])
async def chat_completion(
    request: ChatRequest,
    api_key: str = Depends(verify_api_key)
):
    """
    Main chat completion endpoint with context enrichment
    """
    try:
        # Enrich prompt with context
        enriched_messages, quilt_metadata = enrich_prompt(
            user_id=request.user_id,
            application=request.application,
            messages=request.messages,
            enrichment_config=request.context_enrichment
        )
        
        # Convert messages to OpenAI format
        openai_messages = [
            {"role": msg.role, "content": msg.content}
            for msg in enriched_messages
        ]
        
        # Call OpenAI API
        start_time = datetime.utcnow()
        completion = await openai_client.chat.completions.create(
            model=request.model,
            messages=openai_messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens
        )
        end_time = datetime.utcnow()
        
        # Extract response
        assistant_message = completion.choices[0].message.content
        
        # Calculate metadata
        latency_ms = int((end_time - start_time).total_seconds() * 1000)
        tokens_used = completion.usage.total_tokens
        
        # Rough cost estimation (GPT-4 pricing)
        cost_per_1k_tokens = 0.03  # Simplified
        cost_usd = (tokens_used / 1000) * cost_per_1k_tokens
        
        # Save interaction to history
        save_interaction(
            user_id=request.user_id,
            messages=request.messages,
            response=assistant_message,
            metadata={
                "application": request.application,
                "model": request.model,
                "summary": request.messages[-1].content[:100]  # First 100 chars of last message
            }
        )
        
        # Build response
        response = ChatResponse(
            id=completion.id,
            model_used=request.model,
            context_quilt=quilt_metadata,
            response=Message(role="assistant", content=assistant_message),
            metadata={
                "latency_ms": latency_ms,
                "tokens_used": tokens_used,
                "cost_usd": round(cost_usd, 4),
                "ab_variant": "control"  # TODO: Implement A/B testing
            }
        )
        
        return response
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing request: {str(e)}")


@app.get("/v1/memory/{user_id}", response_model=MemoryResponse, tags=["Memory"])
async def get_memory(
    user_id: str,
    api_key: str = Depends(verify_api_key)
):
    """
    Retrieve user memory and context
    """
    memory = get_user_memory(user_id)
    history = conversation_history.get(user_id, [])
    
    last_interaction = None
    if history:
        last_interaction = history[-1]["timestamp"]
    
    return MemoryResponse(
        user_id=user_id,
        profile=memory,
        history_count=len(history),
        last_interaction=last_interaction
    )


@app.put("/v1/memory/{user_id}", tags=["Memory"])
async def update_memory(
    user_id: str,
    memory_update: Dict[str, Any],
    api_key: str = Depends(verify_api_key)
):
    """
    Update user memory/preferences
    """
    memory = get_user_memory(user_id)
    
    # Update memory fields
    for key, value in memory_update.items():
        if key in ["profile", "preferences", "context"]:
            memory[key].update(value)
    
    user_memories[user_id] = memory
    
    return {"status": "success", "user_id": user_id, "updated_fields": list(memory_update.keys())}


# ============================================
# Application Startup/Shutdown
# ============================================

@app.on_event("startup")
async def startup_event():
    """Initialize services on startup"""
    print("🧵 Context Quilt starting up...")
    print("✅ OpenAI client initialized")
    # TODO: Initialize PostgreSQL connection
    # TODO: Initialize Redis connection
    print("🚀 Context Quilt is ready!")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    print("🧵 Context Quilt shutting down...")
    # TODO: Close database connections
    print("👋 Context Quilt stopped")


# ============================================
# Development: Run with uvicorn
# ============================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
