import os
import httpx
from typing import Optional


class RpcPlaywrightClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0):
        self.base_url = base_url or os.getenv("PLAYWRIGHT_RPC_URL", "http://localhost:3000/rpc")
        self.timeout = timeout
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def get_html(self, url: str) -> Optional[str]:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "getHTML",
            "params": {"url": url}
        }
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.base_url, json=payload)
            
            if response.status_code != 200:
                raise Exception(f"HTTP {response.status_code}: {response.text}")
            
            try:
                data = response.json()
            except Exception as e:
                raise Exception(f"Invalid JSON response: {e}")
            
            if "error" in data:
                error = data["error"]
                message = error.get("message", "Unknown error")
                raise Exception(f"JSON-RPC error: {message}")
            
            result = data.get("result")
            if isinstance(result, str):
                return result
            elif isinstance(result, dict) and "html" in result:
                return result["html"]
            else:
                return None
