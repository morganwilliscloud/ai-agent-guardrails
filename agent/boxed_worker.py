"""One agent turn. Only runtime_app.py may launch this inside Box."""
import json
import os
from pathlib import Path
import sys

# Isolated Python (-I) deliberately ignores PYTHONPATH and the script directory.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import agent
from strands.tools.mcp.mcp_client import MCPClient


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    agent._config = request["config"]
    headers = {
        "Authorization": "Bearer " + os.environ["CUSTOMER_TOKEN"],
        "x-amzn-bedrock-agentcore-policy-session-id": request["session_id"],
    }
    with MCPClient(url=request["config"]["gateway-url"], headers=headers) as client:
        tools = client.list_tools_sync()
        assistant = agent.create_agent(tools, request["session_id"], request["actor_id"])
        result = assistant(request["prompt"])
        response = agent.extract_response_text(result)
        Path(sys.argv[2]).write_text(json.dumps({"response": response}))


if __name__ == "__main__":
    main()
