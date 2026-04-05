from job_hunting.models import Scrape
from job_hunting.lib.scrapers.html_cleaner import clean_html_to_markdown


class GenericScraper:
    def __init__(self, browser, url):
        self.url = url
        self.browser = browser

    async def process(self):
        scrape, is_new = Scrape.first_or_initialize(
            url=self.url,
        )

        if is_new or scrape.html is None:
            print("contents needs to be downloaded")
            html_content = await self.browser.get_page_content(self.url)
            scrape.html = html_content
        else:
            print("contents already downloaded")
            html_content = scrape.html
            await self.browser.page.goto(
                self.url,
                wait_until="domcontentloaded",
            )

        scrape.job_content = clean_html_to_markdown(html_content)
        scrape.save()
        return scrape
