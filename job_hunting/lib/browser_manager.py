# BrowserManager
# Standalone HTTP server for browser scraping
import json
import asyncio
from aiohttp import web
from playwright.async_api import async_playwright, TimeoutError


class BrowserManager:
    def __init__(self, cookies_file="cookies.json"):
        self.playwright = None
        self.browser = None
        self.context = None
        self.cookies_file = cookies_file
        self.page = None

    async def start_browser(self, headless=False, navigation_timeout=30000):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=headless)
        self.context = await self.browser.new_context()
        self.context.set_default_navigation_timeout(navigation_timeout)
        self.context.set_default_timeout(navigation_timeout)
        self.page = await self.context.new_page()
        await self.load_cookies()

    async def close_browser(self):
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def get_page_content(self, url, timeout=30000) -> str:
        try:
            await self.page.goto(url, wait_until="networkidle", timeout=timeout)
        except TimeoutError:
            try:
                await self.page.evaluate("window.stop()")
            except Exception:
                pass
            print(f"Timeout exceeded while navigating to {url}")
        except Exception as e:
            print("*" * 88)
            print(f"Error navigating to {url}: {e}")
            print("*" * 88)
            return None
        return await self.page.content()

    async def save_cookies(self):
        cookies = await self.context.cookies()
        with open(self.cookies_file, "w") as f:
            json.dump(cookies, f)

    async def load_cookies(self):
        try:
            with open(self.cookies_file, "r") as f:
                cookies = json.load(f)
                await self.context.add_cookies(cookies)
        except FileNotFoundError:
            pass

    async def handle_scrape_request(self, request):
        """Handle HTTP POST requests to scrape URLs"""
        try:
            data = await request.json()
            url = data.get("url")
            
            if not url:
                return web.json_response(
                    {"error": "URL is required"}, 
                    status=400
                )
            
            print(f"Scraping URL: {url}")
            html_content = await self.get_page_content(url)
            
            if html_content is None:
                return web.json_response(
                    {"error": "Failed to fetch content"}, 
                    status=500
                )
            
            return web.json_response({"html": html_content})
            
        except Exception as e:
            print(f"Error handling scrape request: {e}")
            return web.json_response(
                {"error": str(e)}, 
                status=500
            )

    async def start_server(self, port=8888):
        """Start the HTTP server"""
        app = web.Application()
        app.router.add_post('/', self.handle_scrape_request)
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        site = web.TCPSite(runner, 'localhost', port)
        await site.start()
        
        print(f"Browser service started on http://localhost:{port}")
        return runner

    async def run_server(self, port=8888):
        """Run the complete browser service"""
        # Start browser in non-headless mode
        await self.start_browser(headless=False)
        
        # Start HTTP server
        runner = await self.start_server(port)
        
        try:
            # Keep server running
            print("Browser service is running. Press Ctrl+C to stop.")
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("Shutting down browser service...")
        finally:
            await runner.cleanup()
            await self.close_browser()


async def main():
    """Main entry point for running as standalone service"""
    browser_manager = BrowserManager()
    await browser_manager.run_server()


if __name__ == "__main__":
    asyncio.run(main())
