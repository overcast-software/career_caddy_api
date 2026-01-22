import os
import httpx
from typing import Optional, Dict, Any


class RpcPlaywrightClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0):
        self.base_url = base_url or os.getenv(
            "PLAYWRIGHT_RPC_URL", "http://localhost:8001"
        )
        self.timeout = timeout

    async def get_html(
        self, url: str, credentials: Optional[Dict[str, str]] = None
    ) -> Optional[str]:
        """Get HTML content from a URL using the new scraper API"""
        return await self.scrape(url, format="html", credentials=credentials)

    async def get_markdown(
        self, url: str, credentials: Optional[Dict[str, str]] = None
    ) -> Optional[str]:
        """Get markdown content from a URL using the new scraper API"""
        return await self.scrape(url, format="markdown", credentials=credentials)

    async def scrape(
        self,
        url: str,
        format: str = "html",
        credentials: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        """Scrape a URL and return content in the specified format"""
        payload = {
            "url": url,
            "format": format,
        }

        if credentials:
            payload["credentials"] = credentials

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/scrape", json=payload)

            if response.status_code != 200:
                raise Exception(f"HTTP {response.status_code}: {response.text}")

            try:
                data = response.json()
            except Exception as e:
                raise Exception(f"Invalid JSON response: {e}")

            if not data.get("success", False):
                error_msg = data.get("error", "Unknown error")
                raise Exception(f"Scraper error: {error_msg}")

            return data.get("content")

    async def health_check(self) -> Dict[str, Any]:
        """Check if the scraper service is healthy"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/health")

            if response.status_code != 200:
                raise Exception(f"Health check failed: HTTP {response.status_code}")

            return response.json()
