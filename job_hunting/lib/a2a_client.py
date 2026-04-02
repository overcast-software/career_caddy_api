import os
import time
import uuid
import httpx
from typing import Optional


class A2AClient:
    """
    Generic A2A (Agent-to-Agent) JSON-RPC 2.0 HTTP client.

    Sends a natural-language message to an A2A-compatible agent service,
    polls until the task completes, and returns the text response.

    Follows the same pattern as RpcPlaywrightClient.
    """

    def __init__(self, base_url: Optional[str] = None, timeout: float = 60.0):
        self.base_url = base_url or os.getenv("A2A_AGENT_URL", "http://localhost:3012")
        self.timeout = timeout

    def send(self, message: str) -> str:
        """
        Send a message to the agent and block until the task completes.
        Returns the agent's text response.
        Raises on HTTP errors, failed tasks, or timeout.
        """
        task_id = self._send_message(message)
        return self._poll_task(task_id)

    def health_check(self) -> dict:
        """Fetch the agent card from /.well-known/agent-card.json."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}/.well-known/agent-card.json")
            if response.status_code != 200:
                raise Exception(f"Health check failed: HTTP {response.status_code}")
            return response.json()

    def _send_message(self, message: str) -> str:
        payload = {
            "jsonrpc": "2.0",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": message}],
                    "kind": "message",
                    "messageId": str(uuid.uuid4()),
                }
            },
            "id": str(uuid.uuid4()),
        }

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(self.base_url, json=payload)

        if response.status_code != 200:
            raise Exception(f"A2A message/send failed: HTTP {response.status_code}: {response.text}")

        data = response.json()
        if "error" in data:
            raise Exception(f"A2A error: {data['error']}")

        result = data.get("result", {})
        task_id = result.get("id")
        if not task_id:
            raise Exception("A2A response missing task id")

        # If already completed synchronously, extract text immediately
        status = result.get("status", {})
        if status.get("state") in ("completed", "failed", "canceled"):
            return self._extract_text(result)

        return task_id

    def _poll_task(self, task_id: str) -> str:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            payload = {
                "jsonrpc": "2.0",
                "method": "tasks/get",
                "params": {"id": task_id},
                "id": str(uuid.uuid4()),
            }

            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self.base_url, json=payload)

            if response.status_code != 200:
                raise Exception(f"A2A tasks/get failed: HTTP {response.status_code}: {response.text}")

            data = response.json()
            if "error" in data:
                raise Exception(f"A2A tasks/get error: {data['error']}")

            result = data.get("result", {})
            state = result.get("status", {}).get("state")

            if state == "completed":
                return self._extract_text(result)
            if state in ("failed", "canceled"):
                raise Exception(f"A2A task {state}: {result.get('status', {}).get('message', '')}")

            time.sleep(1)

        raise Exception(f"A2A task {task_id} timed out after {self.timeout}s")

    def _extract_text(self, result: dict) -> str:
        # Try history last message parts first
        history = result.get("history", [])
        if history:
            last = history[-1]
            for part in last.get("parts", []):
                if part.get("kind") == "text":
                    return part["text"]

        # Fall back to artifacts
        for artifact in result.get("artifacts", []):
            for part in artifact.get("parts", []):
                if part.get("kind") == "text":
                    return part["text"]

        return ""


def get_browser_agent_client() -> A2AClient:
    url = os.getenv("BROWSER_AGENT_URL", "http://localhost:3012")
    return A2AClient(base_url=url)


def get_orchestrator_client() -> A2AClient:
    url = os.getenv("ORCHESTRATOR_AGENT_URL", "http://localhost:3011")
    return A2AClient(base_url=url)
