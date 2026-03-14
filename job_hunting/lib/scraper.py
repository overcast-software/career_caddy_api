import requests
from typing import Optional


"""Shared Pydantic models for job and company data."""

from job_hunting.lib.validations.job_post_data import JobPostData


class Scraper:
    def __init__(self, browser_service_url: str, url: str, scrape_id: Optional[int] = None):
        self.url = url
        self.browser_service_url = browser_service_url
        self.scrape_id = scrape_id

    def process(self, url: Optional[str] = None) -> JobPostData:
        """
        Scrape a job posting URL and return structured job data.

        Args:
            url: The job posting URL to scrape. If not provided, uses self.url.

        Returns:
            JobPostData: Structured job posting data

        Raises:
            requests.RequestException: If the HTTP request fails
            ValueError: If the response cannot be parsed into JobPostData
        """
        target_url = url or self.url

        # Include scrape_id in the request if available
        payload = {"url": target_url}
        if self.scrape_id is not None:
            payload["scrape_id"] = self.scrape_id
        
        response = requests.post(
            f"{self.browser_service_url}/scrape_job",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=300,
        )
        response.raise_for_status()
        jpd = JobPostData(**response.json())
        print(jpd)
        return jpd
