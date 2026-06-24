import os
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client

# Create model instance for agent
model = BedrockModel(model_id="us.anthropic.claude-sonnet-4-20250514-v1:0")

# Connect to MCP server(s) for tools
mcp = MCPClient(lambda: streamablehttp_client(mcp_server_url, headers={"Authorization": f"Bearer {token}"}))

with mcp:
    tools = mcp.list_tools_sync()
    print(f"Loaded {len(tools)} tools")

    # Create agent with model and tools
    agent = Agent(model=model, tools=tools)

    # Interact with agent
    while True:
        prompt = input("\nYou: ")
        if prompt.lower() in ("quit", "exit"):
            break
        print("\nAgent:", agent(prompt))
