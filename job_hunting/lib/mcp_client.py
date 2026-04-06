import os
import asyncio
from typing import Optional, Dict, Any
import logging

# Requires: pip install mcp
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession

logger = logging.getLogger(__name__)


class MCPClient:
    """
    Minimal MCP client wrapper for calling a tool on an MCP server via SSE.
    Fire-and-forget by default; returns whatever the server returns but callers
    may choose to ignore.
    """

    def __init__(self, sse_url: str, tool_name: Optional[str] = None, timeout: int = 300):
        self.sse_url = sse_url
        self.tool_name = tool_name
        self.timeout = timeout
        # Low-level tool names (override via env if your server uses different names)
        self.create_tab_tool = os.getenv("BROWSER_MCP_CREATE_TAB_TOOL", "create_tab")
        self.nav_snapshot_tool = os.getenv("BROWSER_MCP_NAV_SNAPSHOT_TOOL", "navigate_and_snapshot")
        self.close_tab_tool = os.getenv("BROWSER_MCP_CLOSE_TAB_TOOL", "close_tab")
        logger.debug(
            "MCPClient configured sse_url=%s single_tool=%s create_tab_tool=%s nav_snapshot_tool=%s close_tab_tool=%s timeout=%s",
            self.sse_url,
            bool(self.tool_name),
            self.create_tab_tool,
            self.nav_snapshot_tool,
            self.close_tab_tool,
            self.timeout,
        )

    def scrape(self, url: str, scrape_id: Optional[int] = None) -> Any:
        logger.info("MCPClient.scrape start url=%s scrape_id=%s mode=%s", url, scrape_id, ("single_tool" if self.tool_name else "flow"))
        # If a specific tool_name is configured, call it directly; else run the tab flow.
        if self.tool_name:
            args: Dict[str, Any] = {"url": url}
            if scrape_id is not None:
                args["scrape_id"] = scrape_id
            return self._call_tool(self.tool_name, args)
        # Orchestrate: create_tab -> navigate_and_snapshot -> close_tab
        return asyncio.run(self._abrowse(url, scrape_id))

    def _call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        return asyncio.run(self._acall_tool(tool_name, arguments))

    def _summarize_result(self, result: Any) -> str:
        try:
            content = getattr(result, "content", None)
            count = len(content) if isinstance(content, list) else (1 if content else 0)
            return f"type={type(result).__name__} content_items={count}"
        except Exception:
            return f"type={type(result).__name__}"

    async def _acall_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        # Connect to the SSE MCP server and invoke the tool
        async with sse_client(self.sse_url) as (read, write):
            async with ClientSession(read, write) as session:
                logger.debug("MCP connect sse_url=%s", self.sse_url)
                # MCP handshake — must initialize before any requests
                await session.initialize()
                logger.debug("MCP session initialized")
                # Optionally, you could inspect available tools:
                # tools = await session.list_tools()
                # Invoke the tool
                logger.info("MCP call_tool name=%s arg_keys=%s", tool_name, list(arguments.keys()))
                result = await session.call_tool(tool_name, arguments)
                logger.debug("MCP call_tool name=%s result=%s", tool_name, self._summarize_result(result))
                return result

    async def _abrowse(self, url: str, scrape_id: Optional[int] = None) -> Any:
        async with sse_client(self.sse_url) as (read, write):
            async with ClientSession(read, write) as session:
                logger.debug("MCP connect sse_url=%s", self.sse_url)
                # MCP handshake — must initialize before any requests
                await session.initialize()
                logger.debug("MCP session initialized")
                # 1) create_tab
                logger.info("MCP flow: create_tab")
                tab_res = await session.call_tool(self.create_tab_tool, {})
                logger.debug("MCP create_tab result=%s", self._summarize_result(tab_res))
                tab_id = self._extract_tab_id(tab_res) or self._first_text(tab_res)
                if not tab_id:
                    logger.error("MCP flow: no tab_id from create_tab; result=%s", self._summarize_result(tab_res))
                    raise ValueError("MCP create_tab did not return a tab_id")

                logger.info("MCP flow: navigate_and_snapshot tab_id=%s url=%s", tab_id, url)
                nav_res = None
                try:
                    # 2) navigate_and_snapshot
                    nav_res = await session.call_tool(self.nav_snapshot_tool, {"tab_id": tab_id, "url": url})
                    logger.debug("MCP navigate_and_snapshot result=%s", self._summarize_result(nav_res))
                finally:
                    # 3) close_tab (best-effort)
                    logger.info("MCP flow: close_tab tab_id=%s", tab_id)
                    try:
                        await session.call_tool(self.close_tab_tool, {"tab_id": tab_id})
                    except Exception:
                        logger.exception("MCP flow: close_tab failed (tab_id=%s)", tab_id)
                logger.info("MCP flow: completed url=%s tab_id=%s", url, tab_id)
                return self._extract_content(nav_res) if nav_res is not None else None

    def _extract_content(self, result: Any) -> Optional[str]:
        """Extract job content from navigate_and_snapshot result.
        The tool returns JSON: {"title": ..., "url": ..., "status": ..., "content": "page text"}.
        Falls back to raw text if JSON parsing fails.
        """
        import json as _json
        raw = self._first_text(result)
        if not raw:
            return None
        try:
            data = _json.loads(raw)
            return data.get("content") or raw
        except Exception:
            return raw

    def _first_text(self, result: Any) -> Optional[str]:
        content = getattr(result, "content", None)
        if isinstance(content, list) and content:
            # Try object attribute, then dict access
            txt = getattr(content[0], "text", None)
            if isinstance(txt, str) and txt.strip():
                return txt.strip()
            if isinstance(content[0], dict):
                t = content[0].get("text")
                if isinstance(t, str) and t.strip():
                    return t.strip()
        return None

    def _extract_tab_id(self, result: Any) -> Optional[str]:
        content = getattr(result, "content", None)
        if not isinstance(content, list):
            return None
        for item in content:
            # JSON-like payload
            json_val = getattr(item, "json", None)
            if isinstance(item, dict):
                json_val = json_val or item.get("json")
            if isinstance(json_val, dict):
                tab = json_val.get("tab_id") or json_val.get("tabId")
                if isinstance(tab, str) and tab.strip():
                    return tab.strip()
            # Text fallback if it clearly looks like a tab id
            text_val = getattr(item, "text", None)
            if isinstance(item, dict):
                text_val = text_val or item.get("text")
            if isinstance(text_val, str) and text_val.strip():
                s = text_val.strip().strip('"')
                if s.lower().startswith("tab"):
                    return s
        return None


def get_browser_mcp_client() -> MCPClient:
    """
    Factory for the browser MCP client.
    Env:
      - BROWSER_MCP_SSE_URL (default: http://0.0.0.0:3004/sse)
      - BROWSER_MCP_TOOL_NAME (optional: if set, uses single-tool mode instead of flow)
    """
    sse_url = os.getenv("BROWSER_MCP_SSE_URL", "http://0.0.0.0:3004/sse")
    tool_name = os.getenv("BROWSER_MCP_TOOL_NAME") or None
    return MCPClient(sse_url=sse_url, tool_name=tool_name)
