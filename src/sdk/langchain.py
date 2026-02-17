"""
Context Quilt - LangChain SDK Adapter
Provides 'Plug-and-Play' Memory for Agents
"""

from typing import Any, Dict, List, Optional, Union
from uuid import UUID
import requests
import json
import os

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from langchain_core.tools import Tool

class ContextQuiltCallbackHandler(BaseCallbackHandler):
    """
    Callback Handler that captures the full Agent Execution Trace
    and sends it to Context Quilt for passive learning.
    """
    
    def __init__(self, api_url: str, user_id: str, app_id: str):
        self.api_url = api_url.rstrip("/")
        self.user_id = user_id
        self.app_id = app_id
        self.trace = []
        self.current_step = 0
        
    def on_llm_start(
        self, serialized: Dict[str, Any], prompts: List[str], **kwargs: Any
    ) -> None:
        """Capture LLM input (Thought)"""
        self.current_step += 1
        self.trace.append({
            "step": self.current_step,
            "type": "thought",
            "content": prompts[0] if prompts else ""
        })

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Capture LLM output"""
        text = response.generations[0][0].text
        self.trace[-1]["output"] = text

    def on_tool_start(
        self, serialized: Dict[str, Any], input_str: str, **kwargs: Any
    ) -> None:
        """Capture Tool usage"""
        self.current_step += 1
        self.trace.append({
            "step": self.current_step,
            "type": "tool_call",
            "tool_name": serialized.get("name"),
            "tool_args": input_str
        })

    def on_tool_end(self, output: str, **kwargs: Any) -> None:
        """Capture Tool output"""
        self.trace[-1]["tool_output"] = output

    def on_chain_end(self, outputs: Dict[str, Any], **kwargs: Any) -> None:
        """End of execution - Send trace to Context Quilt"""
        payload = {
            "user_id": self.user_id,
            "interaction_type": "trace",
            "execution_trace": self.trace,
            "output": outputs
        }
        
        try:
            requests.post(
                f"{self.api_url}/v1/memory",
                json=payload,
                headers={"X-App-ID": self.app_id},
                timeout=5
            )
        except Exception as e:
            print(f"ContextQuilt Error: Failed to save trace - {e}")

def save_user_context(fact: str, category: str, confidence: float = 1.0) -> str:
    """
    Save a new fact about the user to their long-term memory.
    Use this when you learn something new about the user's preferences, history, or identity.
    """
    # This function is a stub that will be replaced by the agent's executor
    # or intercepted if running locally. In a real SDK, this would make an API call.
    # For the 'Tool' definition, we just need the signature.
    return "Fact saved."

def get_memory_tool(api_url: str, user_id: str, app_id: str) -> Tool:
    """
    Factory to create the 'save_user_context' tool bound to the API.
    """
    def _save_context(input_str: str):
        # Parse input (assuming JSON or simple string)
        # For simplicity, we'll assume the agent passes a JSON string or we parse it
        try:
            data = json.loads(input_str)
            fact = data.get("fact")
            category = data.get("category", "general")
        except:
            fact = input_str
            category = "general"
            
        payload = {
            "user_id": user_id,
            "interaction_type": "tool_call",
            "fact": fact,
            "category": category,
            "confidence": 1.0
        }
        
        try:
            requests.post(
                f"{api_url}/v1/memory",
                json=payload,
                headers={"X-App-ID": app_id},
                timeout=5
            )
            return f"Successfully saved fact: {fact}"
        except Exception as e:
            return f"Error saving fact: {e}"

    return Tool(
        name="save_user_context",
        func=_save_context,
        description="Call this function whenever you learn a new fact about the user. Input should be a JSON string with 'fact' and 'category'."
    )
