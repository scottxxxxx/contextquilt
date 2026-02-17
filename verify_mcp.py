import asyncio
import sys
import os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def verify_mcp_server():
    print("Starting MCP Server Verification...")
    
    # Server parameters
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "contextquilt.mcp_server.server"],
        env=os.environ.copy()
    )
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # Initialize
            await session.initialize()
            
            # 1. List Tools
            print("\n--- Listing Tools ---")
            tools = await session.list_tools()
            for tool in tools.tools:
                print(f"- {tool.name}: {tool.description}")
            
            # 2. Call Tool: get_user_context
            print("\n--- Calling get_user_context ---")
            result = await session.call_tool(
                "get_user_context",
                arguments={"user_id": "user_123"}
            )
            print(f"Result: {result.content[0].text}")
            
            # 3. Call Tool: update_user_memory
            print("\n--- Calling update_user_memory ---")
            result = await session.call_tool(
                "update_user_memory",
                arguments={
                    "user_id": "user_123",
                    "conversation_log": "User mentioned they like Python.",
                    "facts": ["likes Python"]
                }
            )
            print(f"Result: {result.content[0].text}")
            
            # 4. List Resources
            print("\n--- Listing Resources ---")
            resources = await session.list_resources()
            for resource in resources.resources:
                print(f"- {resource.name}: {resource.uri}")
                
            # 5. Read Resource
            print("\n--- Reading Resource ---")
            # Note: In the mock implementation, the URI template is contextquilt://users/{user_id}/profile
            # But list_resources returns a concrete example or template? 
            # The implementation returns a template-like URI in list_resources, 
            # but read_resource expects a concrete one.
            # Let's try a concrete one.
            try:
                resource_content = await session.read_resource("contextquilt://users/user_123/profile")
                print(f"DEBUG: {resource_content}")
                print(f"Content: {resource_content.contents[0].text}")
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"Error reading resource: {e}")

    print("\nVerification Complete!")

if __name__ == "__main__":
    # Add src to python path for the subprocess
    sys.path.append(os.path.join(os.getcwd(), "src"))
    asyncio.run(verify_mcp_server())
