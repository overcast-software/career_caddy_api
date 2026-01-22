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
        scrape, is_new = Scrape.first_or_initialize(url=self.url)

        if is_new or scrape.html is None:
            print("contents needs to be downloaded")
            # Note: This will need to be updated when RpcPlaywrightClient is made synchronous
            html = self.rpc_client.get_html(self.url)
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
