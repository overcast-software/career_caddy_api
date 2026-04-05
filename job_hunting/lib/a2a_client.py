import logging
import os
import time
import uuid
import httpx
from typing import Optional

logger = logging.getLogger(__name__)


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
        logger.info("A2AClient.send base_url=%s message_len=%s", self.base_url, len(message))
        logger.debug("A2AClient.send message=%s", message)
        task_id = self._send_message(message)
        logger.info("A2AClient.send task_id=%s polling...", task_id)
        result = self._poll_task(task_id)
        logger.info("A2AClient.send task_id=%s completed result_len=%s", task_id, len(result))
        return result

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

        logger.debug("A2AClient._send_message POST %s payload_id=%s", self.base_url, payload["id"])
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(self.base_url, json=payload)

        logger.debug("A2AClient._send_message response status=%s", response.status_code)
        if response.status_code != 200:
            logger.error("A2AClient._send_message HTTP %s: %s", response.status_code, response.text)
            raise Exception(f"A2A message/send failed: HTTP {response.status_code}: {response.text}")

        data = response.json()
        if "error" in data:
            logger.error("A2AClient._send_message error=%s", data["error"])
            raise Exception(f"A2A error: {data['error']}")

        result = data.get("result", {})
        task_id = result.get("id")
        if not task_id:
            logger.error("A2AClient._send_message missing task id, result keys=%s", list(result.keys()))
            raise Exception("A2A response missing task id")

        # If already completed synchronously, extract text immediately
        status = result.get("status", {})
        state = status.get("state")
        logger.info("A2AClient._send_message task_id=%s initial_state=%s", task_id, state)
        if state in ("completed", "failed", "canceled"):
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
            logger.debug("A2AClient._poll_task task_id=%s state=%s", task_id, state)

            if state == "completed":
                return self._extract_text(result)
            if state in ("failed", "canceled"):
                msg = result.get("status", {}).get("message", "")
                logger.error("A2AClient._poll_task task_id=%s state=%s message=%s", task_id, state, msg)
                raise Exception(f"A2A task {state}: {msg}")

            time.sleep(1)

        logger.error("A2AClient._poll_task task_id=%s timed out after %ss", task_id, self.timeout)
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


def get_caddy_agent_client() -> A2AClient:
    url = os.getenv("CADDY_AGENT_URL", "http://localhost:3011")
    return A2AClient(base_url=url)
