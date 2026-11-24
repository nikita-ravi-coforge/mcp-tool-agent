"""Run sample requests from data/test_requests.json through the live agent and print each ReAct trace.

    uv run --env-file .env python scripts/demo.py                 # the default four
    uv run --env-file .env python scripts/demo.py REQ-5007 REQ-5012
"""

import asyncio
import json
import sys
from pathlib import Path

from equipment import agent, tracing

# One approve, one deny, and two escalations for different reasons (spec above tier; lost laptop).
DEFAULT = ["REQ-5001", "REQ-5002", "REQ-5006", "REQ-5014"]

requests = json.loads((Path(__file__).parents[1] / "data" / "test_requests.json").read_text())["requests"]
by_id = {r["request_id"]: r for r in requests}

tracing.setup_tracing("equipment-agent")
for request_id in sys.argv[1:] or DEFAULT:
    request = by_id[request_id]
    print(f"\n{'=' * 100}\n{request_id}  {request['employee_id']}: {request['text']}")
    print(f"expected: {request['expected']['decision']} (rule {request['expected']['rule']})\n{'-' * 100}")
    print(agent.format_trace(asyncio.run(agent.handle(request["employee_id"], request["text"]))))
