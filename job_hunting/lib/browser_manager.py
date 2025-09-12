# BrowserManager
# Sensible defaults for typical scraping
import json
from playwright.async_api import async_playwright, TimeoutError


class BrowserManager:
    def __init__(self, cookies_file="cookies.json"):
        self.playwright = None
        self.browser = None
        self.context = None
        self.cookies_file = cookies_file
        self.page = None

    async def start_browser(self, headless=True, navigation_timeout=30000):
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

    async def get_page_content(self, url, timeout=3000) -> str:
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
