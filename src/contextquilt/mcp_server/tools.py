"""Tool definitions for the ContextQuilt MCP Server."""

from typing import Any, Dict, List, Optional
import structlog
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource

logger = structlog.get_logger()

# Mock data for demonstration purposes since we don't have a live DB connection in this context
MOCK_USERS = {
    "user_123": {
        "profile": {
            "name": "Alice",
            "preferences": {"coffee": "dislike", "theme": "dark"},
            "role": "developer"
        },
        "history": [
            {"role": "user", "content": "I hate coffee."},
            {"role": "assistant", "content": "Noted. I will remember that you dislike coffee."}
        ]
    }
}

async def get_user_profile(user_id: str, include_history: bool = True) -> List[TextContent | ImageContent | EmbeddedResource]:
    """
    Get comprehensive user profile from ContextQuilt's longitudinal memory.
    """
    logger.info("get_user_profile", user_id=user_id, include_history=include_history)
    
    # In a real implementation, this would call:
    # memory_layer.retrieve_context(conversation_id="mcp-session", user_id=user_id)
    
    user_data = MOCK_USERS.get(user_id)
    if not user_data:
        return [TextContent(type="text", text=f"User {user_id} not found.")]
    
    profile_text = f"User Profile for {user_id}:\n"
    profile_text += f"Name: {user_data['profile']['name']}\n"
    profile_text += f"Role: {user_data['profile']['role']}\n"
    profile_text += f"Preferences: {user_data['profile']['preferences']}\n"
    
    if include_history:
        profile_text += "\nRecent History:\n"
        for msg in user_data['history']:
            profile_text += f"- {msg['role']}: {msg['content']}\n"
            
    return [TextContent(type="text", text=profile_text)]

async def update_user_memory(user_id: str, conversation_log: str, facts: List[str] = []) -> List[TextContent | ImageContent | EmbeddedResource]:
    """
    Update user memory with new conversation or facts.
    """
    logger.info("update_user_memory", user_id=user_id, facts_count=len(facts))
    
    # In a real implementation, this would call:
    # memory_layer.store_memory(...)
    
    if user_id not in MOCK_USERS:
        MOCK_USERS[user_id] = {"profile": {}, "history": []}
        
    # Mock update
    MOCK_USERS[user_id]["history"].append({"role": "system", "content": f"Memory update: {conversation_log}"})
    if facts:
         MOCK_USERS[user_id]["profile"].setdefault("facts", []).extend(facts)
         
    return [TextContent(type="text", text=f"Memory updated for user {user_id}. Added {len(facts)} facts.")]

async def search_user_history(user_id: str, query: str) -> List[TextContent | ImageContent | EmbeddedResource]:
    """
    Search through user's conversation history.
    """
    logger.info("search_user_history", user_id=user_id, query=query)
    
    user_data = MOCK_USERS.get(user_id)
    if not user_data:
        return [TextContent(type="text", text=f"User {user_id} not found.")]
        
    # Mock search
    results = []
    for msg in user_data['history']:
        if query.lower() in msg['content'].lower():
            results.append(f"{msg['role']}: {msg['content']}")
            
    if not results:
        return [TextContent(type="text", text="No matching history found.")]
        
    return [TextContent(type="text", text="\n".join(results))]
