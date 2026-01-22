import asyncio
from job_hunting.lib.parsers.generic_parser import GenericParser
from job_hunting.lib.remote_playwright_client import RpcPlaywrightClient
from job_hunting.lib.models import Scrape
from markdownify import markdownify as md


class GenericService:
    def __init__(self, url, ai_client, rpc_client=None, creds={}):
        self.url = url
        self.creds = creds
        self.rpc_client = rpc_client or RpcPlaywrightClient()
        self.parser = GenericParser(ai_client)
        self.scrape = None

    def process(self) -> Scrape:
        try:
            # Try to get the current event loop
            loop = asyncio.get_running_loop()
            # If we get here, there's already a running loop
            # We need to run in a thread pool to avoid "already running" error
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, self._async_process())
                return future.result()
        except RuntimeError:
            # No event loop is running, safe to use asyncio.run()
            return asyncio.run(self._async_process())

    async def _async_process(self) -> Scrape:
        scrape, is_new = Scrape.first_or_initialize(url=self.url)

        if is_new or scrape.html is None:
            print("contents needs to be downloaded")
            html = await self.rpc_client.get_html(self.url)
            scrape.html = html
        else:
            print("contents already downloaded")
            html = scrape.html

        # Convert to markdown
        scrape.job_content = md(html)
        self.parser.parse(scrape)
        scrape.save()

        # Process the scrape with the parser
        self.scrape = scrape
        return scrape
