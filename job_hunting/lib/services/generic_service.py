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

    async def process(self) -> Scrape:
        scrape, is_new = Scrape.first_or_initialize(url=self.url)
        
        if is_new or scrape.html is None:
            print("contents needs to be downloaded")
            # Pass credentials to the new scraper API if available
            html = await self.rpc_client.get_html(self.url, credentials=self.creds if self.creds else None)
            scrape.html = html
        else:
            print("contents already downloaded")
        
        # Convert to markdown
        scrape.job_content = md(scrape.html or "")
        scrape.save()
        
        # Process the scrape with the parser
        self.parser.parse(scrape)
        self.scrape = scrape
        return scrape
