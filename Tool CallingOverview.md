# Tool Calling in Agentic Systems: A Comprehensive Guide

## Overview of Tool Calling Architecture

Tool calling enables LLMs to interact with external systems, APIs, and services. This document explains the mechanics, responsibilities, and implementation patterns for tool providers like ContextQuilt.

## The Three Key Parties

### 1. **Application/Orchestrator** (e.g., LangChain, AutoGen)
- Defines available tools and their descriptions
- Manages the conversation flow
- Routes tool calls between LLM and tools
- Handles error recovery and fallbacks

### 2. **LLM** (e.g., GPT-4, Claude, Gemini)
- Understands tool descriptions
- Decides when tools are needed
- Generates structured tool calls
- Incorporates tool results into responses

### 3. **Tool Provider** (e.g., ContextQuilt)
- Provides clear, actionable tool descriptions
- Exposes reliable, fast APIs
- Returns LLM-friendly responses
- Handles errors gracefully

## Complete Tool Calling Flow

### Step 1: Application Defines Tools

```python
from langchain.tools import BaseTool
from pydantic import BaseModel, Field

class GetUserContextInput(BaseModel):
    """Input schema for getting user context"""
    user_id: str = Field(description="Unique identifier for the user")

class ContextQuiltTool(BaseTool):
    name = "get_user_context"
    description = """Get comprehensive user context including identity, 
    preferences, traits, and conversation history. Use this when you need 
    to understand who you're talking to or personalize the conversation."""
    args_schema = GetUserContextInput
    
    def _run(self, user_id: str):
        # Call ContextQuilt API
        import requests
        response = requests.post(
            "https://api.contextquilt.com/get_context",
            json={"user_id": user_id}
        )
        return response.json()
```

### Step 2: Application Presents Tools to LLM

The application sends a prompt that includes tool definitions:

```
System: You are a helpful assistant with access to these tools:

Tools:
1. get_user_context(user_id: str) -> dict
   Description: Get comprehensive user context including identity, preferences, 
   traits, and conversation history. Use this when you need to understand who 
   you're talking to or personalize the conversation.

When you need to use a tool, respond with a JSON object containing:
{"tool": "tool_name", "tool_input": {"arg1": "value1", ...}}

User: Hi, I need help with my account.
```

### Step 3: LLM's Decision Process

The LLM analyzes:
- User request content and context
- Available tools and their descriptions
- Whether a tool would improve the response

**LLM's internal reasoning:**
```
User: "Hi, I need help with my account."
→ This is a generic request
→ I don't know who this user is
→ get_user_context would help personalize my response
→ I need the user_id to call the tool
→ The application should provide user_id from session context
→ I'll request the tool with the provided user_id
```

### Step 4: LLM Generates Structured Tool Call

The LLM outputs structured JSON:

```json
{
  "tool": "get_user_context",
  "tool_input": {"user_id": "user_123"}
}
```

### Step 5: Application Processes Tool Call

The application:
1. Validates the tool call
2. Executes the corresponding function
3. Waits for the tool response

```python
def handle_tool_call(tool_name, tool_input):
    if tool_name == "get_user_context":
        # Call ContextQuilt API
        result = contextquilt_tool._run(**tool_input)
        return {
            "role": "tool",
            "content": str(result),
            "tool_call_id": tool_call_id
        }
```

### Step 6: Tool Provider (ContextQuilt) Executes Request

**Your API endpoint:**
```python
@app.post("/get_context")
def get_user_context(user_id: str):
    # Retrieve from high-speed cache (Hot Path)
    profile = redis.get(f"user_profile:{user_id}")
    
    if not profile:
        # Fallback or trigger Cold Path processing
        profile = generate_default_profile(user_id)
    
    # Format for LLM consumption
    return {
        "identity": profile.get("identity", {}),
        "preferences": profile.get("preferences", {}),
        "traits": profile.get("traits", {}),
        "recent_conversations": profile.get("recent_summaries", []),
        "guidance": profile.get("behavioral_guidance", "")
    }
```

### Step 7: Application Returns Tool Result to LLM

The application sends the tool result back to the LLM:

```
Tool Result (get_user_context):
{
  "user": {"name": "Jane Doe", "role": "Senior Developer"},
  "preferences": {"communication_style": "technical and concise"},
  "guidance": "Be technical and direct. Provide code examples."
}
```

### Step 8: LLM Generates Final Response

The LLM incorporates the tool result into its response:

```
Assistant: Hi Jane! I see you're a Senior Developer who prefers technical,
direct communication. What specific account issue can I help you with today?
I'll provide code examples if relevant.
```

## Tool Description Best Practices

### Critical Elements of a Good Tool Description

```python
description = """
Retrieve comprehensive longitudinal user context to personalize interactions.

USE THIS TOOL WHEN:
- Starting a conversation with a known user
- You need to adapt your communication style to the user
- You need to recall past interactions with this user
- Personalization would improve the response quality

DO NOT USE THIS TOOL WHEN:
- The user is anonymous or not logged in
- You already have sufficient context about the user
- The conversation is completely transactional

INPUT: user_id (string) - Unique identifier for the user

OUTPUT: JSON with user identity, preferences, traits, and conversation guidance.

EXAMPLE SCENARIOS:
1. Customer support: Know user's expertise level and past issues
2. Sales: Understand user's role, company, and purchase history
3. Education: Adapt to user's learning style and progress

FORMAT NOTES:
- Returns JSON that can be directly injected into prompts
- Includes actionable guidance for the LLM
- Cached for <20ms response time
"""
```

### Tool Description Template

```markdown
## [Tool Name]

[Brief one-line description]

### When to Use
- Scenario 1: [Specific situation]
- Scenario 2: [Specific situation]
- Scenario 3: [Specific situation]

### When NOT to Use
- [Situation where tool is unnecessary]
- [Situation where tool would be harmful]

### Input Parameters
- `param1` (type): [Description and constraints]
- `param2` (type): [Description and constraints]

### Output Format
[Description of output format and key fields]

### Example Output
```json
{
  "key_field": "example value",
  "guidance": "Actionable advice for LLM"
}
```

### Performance Characteristics
- Latency: [Expected response time]
- Cache: [Cache behavior if applicable]
- Rate limits: [Any limitations]
```

## Response Format Optimization

### LLM-Friendly Response Structure

```python
def format_for_llm(profile):
    """Convert internal profile to LLM-friendly format"""
    return f"""
## USER CONTEXT PROFILE

**BASIC INFORMATION**
- Name: {profile.get('name', 'Not specified')}
- Role: {profile.get('role', 'Not specified')}
- Company: {profile.get('company', 'Not specified')}
- Expertise Level: {profile.get('expertise', 'Mixed')}

**COMMUNICATION PREFERENCES**
- Style: {profile.get('comm_style', 'Neutral')}
- Technical Depth: {profile.get('tech_level', 'Moderate')}
- Detail Preference: {profile.get('detail_level', 'Balanced')}
- Formality: {profile.get('formality', 'Professional')}

**RECENT HISTORY (Last 3 interactions)**
{format_recent_interactions(profile.get('recent_interactions', []))}

**ACTIONABLE GUIDANCE**
{profile.get('guidance', 'Use standard professional tone.')}

**KEY FACTS TO REMEMBER**
{format_key_facts(profile.get('key_facts', []))}
"""
```

### Error Handling in Tool Responses

```python
def get_user_context(user_id: str):
    try:
        # Attempt to get from cache
        profile = cache.get(user_id)
        
        if not profile:
            return {
                "status": "partial",
                "message": "User profile not fully loaded",
                "basic_info": get_basic_info(user_id),
                "suggestion": "Ask for additional details or use default context"
            }
        
        return {
            "status": "complete",
            "profile": profile
        }
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"Service temporarily unavailable: {str(e)}",
            "suggestion": "Proceed without user context for now",
            "fallback_context": get_fallback_context()
        }
```

## Advanced Tool Calling Patterns

### Parallel Tool Calling
```json
{
  "tool_calls": [
    {
      "tool": "get_user_context",
      "tool_input": {"user_id": "user_123"}
    },
    {
      "tool": "check_system_status",
      "tool_input": {"system": "account_management"}
    }
  ]
}
```

### Tool Chaining
```python
# Example sequence:
# 1. get_user_context → learns project name
# 2. get_project_details → uses project name from step 1
# 3. generate_report → uses both contexts
```

### Conditional Tool Usage
```python
# The LLM might reason:
"""
User: "Tell me about my project status."
- First, I need to know who this user is → get_user_context
- From context, I learn they're working on "Project Alpha"
- Now I need project details → get_project_details("Project Alpha")
- Combine both to give comprehensive answer
"""
```

## Implementation Checklist for Tool Providers

### ✅ Core Requirements
- [ ] Clear, comprehensive tool description
- [ ] Well-defined input schema
- [ ] LLM-friendly output format
- [ ] Fast response times (<100ms preferred)
- [ ] Reliable error handling
- [ ] Proper authentication/authorization

### ✅ Advanced Features
- [ ] Caching strategy implementation
- [ ] Rate limiting and quotas
- [ ] Usage analytics and logging
- [ ] Versioning support
- [ ] Batch processing options
- [ ] Webhook support for async results

### ✅ Integration Support
- [ ] LangChain integration package
- [ ] AutoGen integration package
- [ ] OpenAI function calling compatibility
- [ ] Claude tool use compatibility
- [ ] REST API documentation
- [ ] SDK libraries (Python, JavaScript)

## Monitoring and Optimization

### Key Metrics to Track
1. **Tool Usage Frequency**
   - How often is your tool called?
   - What prompts trigger tool usage?

2. **Performance Metrics**
   - Response time (P50, P95, P99)
   - Error rates
   - Cache hit ratios

3. **Impact Metrics**
   - Conversation quality improvements
   - Token savings from context injection
   - User satisfaction changes

### Optimization Opportunities
1. **Cache Optimization**
   - Pre-warm caches for frequent users
   - Implement multi-level caching
   - Cache invalidation strategies

2. **Response Optimization**
   - Compress frequently returned data
   - Prioritize most-used fields
   - Implement field-level caching

3. **LLM Guidance Refinement**
   - Analyze which guidance is most effective
   - A/B test different guidance formats
   - Personalize guidance based on LLM model

## Common Pitfalls and Solutions

### Problem: LLM Doesn't Use Your Tool
**Solution:** Improve tool description clarity with specific examples and triggers.

### Problem: LLM Uses Tool Inappropriately
**Solution:** Add "When NOT to use" guidance and refine input validation.

### Problem: Tool Responses Are Too Verbose
**Solution:** Implement response compression and field prioritization.

### Problem: High Latency Affects UX
**Solution:** Optimize cache strategy and implement background processing.

### Problem: Tool Errors Break Conversation
**Solution:** Implement graceful fallbacks and error recovery patterns.

## Example: Complete ContextQuilt Integration

```python
"""
Complete ContextQuilt tool implementation for LangChain
"""

from typing import Type, Optional
from langchain.tools import BaseTool
from pydantic import BaseModel, Field
import requests
import os

class ContextQuiltInput(BaseModel):
    user_id: str = Field(
        description="The unique identifier for the user",
        examples=["user_123", "customer_456"]
    )
    include_history: bool = Field(
        default=True,
        description="Whether to include conversation history"
    )
    max_history_items: int = Field(
        default=5,
        description="Maximum number of history items to include",
        ge=1,
        le=20
    )

class ContextQuiltTool(BaseTool):
    name: str = "get_contextquilt_profile"
    description: str = """
    Retrieve a comprehensive user profile from ContextQuilt's longitudinal memory system.
    
    USE THIS TOOL WHEN:
    1. Starting a new conversation with a known user
    2. You need to personalize your response based on user preferences
    3. You want to recall past interactions with this user
    4. You need to adapt your communication style to the user
    
    INPUT: user_id (required) - The unique user identifier
    
    OUTPUT: A structured profile including:
    - Identity: Basic user information (name, role, company)
    - Preferences: User's stated and inferred preferences
    - Traits: Behavioral characteristics and communication style
    - Guidance: Actionable advice for how to interact with this user
    - Recent History: Summaries of recent conversations
    
    PERFORMANCE: <20ms response time from cache
    """
    args_schema: Type[BaseModel] = ContextQuiltInput
    return_direct: bool = False
    
    def __init__(self, api_key: Optional[str] = None):
        super().__init__()
        self.api_key = api_key or os.getenv("CONTEXTQUILT_API_KEY")
        self.base_url = os.getenv("CONTEXTQUILT_API_URL", "https://api.contextquilt.com")
    
    def _run(self, user_id: str, include_history: bool = True, max_history_items: int = 5) -> str:
        """Execute the tool call"""
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            params = {
                "user_id": user_id,
                "include_history": include_history,
                "max_history_items": max_history_items
            }
            
            response = requests.get(
                f"{self.base_url}/v1/profiles",
                headers=headers,
                params=params,
                timeout=2.0  # 2 second timeout for real-time
            )
            
            if response.status_code == 200:
                return self._format_response(response.json())
            elif response.status_code == 404:
                return f"User {user_id} not found in ContextQuilt. Proceed with standard interaction."
            else:
                return f"ContextQuilt service error: {response.status_code}. Proceed without user context."
                
        except requests.exceptions.Timeout:
            return "ContextQuilt timeout. Using fallback context."
        except Exception as e:
            return f"Error accessing ContextQuilt: {str(e)}. Proceed with basic context."
    
    def _format_response(self, data: dict) -> str:
        """Format the API response for LLM consumption"""
        profile = data.get("profile", {})
        
        lines = [
            "=== CONTEXTQUILT USER PROFILE ===",
            f"User: {profile.get('name', 'Unknown')}",
            f"Role: {profile.get('role', 'Not specified')}",
            f"Company: {profile.get('company', 'Not specified')}",
            "",
            "COMMUNICATION PREFERENCES:",
            f"- Style: {profile.get('communication_style', 'Neutral')}",
            f"- Technical Level: {profile.get('technical_level', 'Moderate')}",
            f"- Formality: {profile.get('formality', 'Professional')}",
            "",
            "BEHAVIORAL GUIDANCE:",
            profile.get('guidance', 'Use standard professional tone.'),
            ""
        ]
        
        if data.get("recent_history"):
            lines.append("RECENT INTERACTIONS:")
            for i, item in enumerate(data["recent_history"][:5], 1):
                lines.append(f"{i}. {item.get('summary', 'No summary')}")
        
        lines.append("\n=== END PROFILE ===")
        
        return "\n".join(lines)
    
    async def _arun(self, user_id: str, include_history: bool = True, max_history_items: int = 5) -> str:
        """Async version of the tool"""
        return self._run(user_id, include_history, max_history_items)
```

## Summary

Tool calling transforms LLMs from conversational agents into actionable systems. As a tool provider:

1. **Your description programs the LLM** - Be explicit about when and how to use your tool
2. **Your API design enables integration** - Provide fast, reliable, LLM-friendly responses
3. **Your error handling maintains flow** - Graceful degradation keeps conversations going
4. **Your caching enables performance** - Hot path/cold path architecture is ideal for agentic systems

ContextQuilt's dual-path architecture is particularly well-suited for tool calling:
- **Hot Path**: <20ms responses for real-time tool calls
- **Cold Path**: Background processing for rich profile building
- **Unified API**: Simple integration for any agent framework

By providing persistent, longitudinal user memory, you enable agents to build real relationships rather than having disconnected conversations.