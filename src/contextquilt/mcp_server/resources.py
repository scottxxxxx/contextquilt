"""Resource definitions for the ContextQuilt MCP Server."""

from typing import Any, Dict, List
import structlog
from mcp.types import Resource, TextResourceContents

logger = structlog.get_logger()

# Re-use mock data from tools (in a real app, this would be a shared service)
from .tools import MOCK_USERS

async def get_user_profile_resource(uri: str) -> List[TextResourceContents]:
    """
    Get user profile as a resource.
    URI format: contextquilt://users/{user_id}/profile
    """
    logger.info("get_user_profile_resource", uri=str(uri))
    
    try:
        # Parse URI
        uri_str = str(uri)
        parts = uri_str.replace("contextquilt://", "").split("/")
        if len(parts) != 3 or parts[0] != "users" or parts[2] != "profile":
            raise ValueError("Invalid URI format")
            
        user_id = parts[1]
        
        user_data = MOCK_USERS.get(user_id)
        if not user_data:
             raise ValueError(f"User {user_id} not found")
             
        import json
        return [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(user_data["profile"], indent=2)
            }
        ]
        
    except Exception as e:
        logger.error("resource_error", error=str(e))
        raise
