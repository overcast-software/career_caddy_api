from job_hunting.lib.models import Scrape, JobPost, Company
import sys
import os
from datetime import datetime, date
from typing import Optional
from pydantic import BaseModel, Field, validator
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.ollama import OllamaProvider
from job_hunting.lib.services.prompt_utils import write_prompt_to_file


class ParsedJobData(BaseModel):
    """Pydantic model for validating parsed job data structure."""
    title: str = Field(..., min_length=1, max_length=500, description="Job title")
    company_name: str = Field(..., min_length=1, max_length=200, description="Company name")
    company_display_name: Optional[str] = Field(None, max_length=200, description="Company display name")
    description: Optional[str] = Field(None, description="Job description")
    posted_date: Optional[datetime] = Field(None, description="Job posting date")
    extraction_date: Optional[datetime] = Field(None, description="Data extraction date")
    
    @validator('title')
    def validate_title(cls, v):
        if not v or not v.strip():
            raise ValueError('Job title cannot be empty')
        return v.strip()
    
    @validator('company_name')
    def validate_company_name(cls, v):
        if not v or not v.strip():
            raise ValueError('Company name cannot be empty')
        return v.strip()
    
    @validator('company_display_name')
    def validate_company_display_name(cls, v):
        if v is not None:
            return v.strip() if v.strip() else None
        return v
    
    @validator('description')
    def validate_description(cls, v):
        if v is not None:
            return v.strip() if v.strip() else None
        return v


class GenericParser:
    def __init__(self, client=None):
        self.client = client
        self.agent = None

    def parse(self, scrape: Scrape):
        validated_data = self.analyze_html_with_ai(scrape)
        self.process_evaluation(scrape, validated_data)

    def get_agent(self):
        """Get or create a Pydantic AI agent for structured job data extraction."""
        if self.agent:
            return self.agent
            
        # Prefer OpenAI if available; otherwise fall back to local Ollama
        try:
            if os.getenv("OPENAI_API_KEY"):
                openai_model = OpenAIResponsesModel("gpt-4o")
                return Agent(openai_model, output_type=ParsedJobData)
        except Exception:
            # Fall back to Ollama if OpenAI model initialization fails
            pass

        ollama_model = OpenAIChatModel(
            model_name="qwen3-coder",
            provider=OllamaProvider(base_url="http://localhost:11434/v1"),
        )
        return Agent(ollama_model, output_type=ParsedJobData)

    def process_evaluation(self, scrape, validated_data: ParsedJobData):
        """
        Process validated job data and save to database
        """
        try:
            print("*" * 88)
            print("save off validated data")
            print("*" * 88)

            # Create or find company using validated data
            company, _ = Company.first_or_create(
                name=validated_data.company_name,
                display_name=validated_data.company_display_name,
            )
            print(f"company id: {company.id}")
            
            # Prepare job post defaults with validated data
            job_defaults = {}
            if validated_data.description:
                job_defaults["description"] = validated_data.description
            if validated_data.posted_date:
                job_defaults["posted_date"] = validated_data.posted_date
            if validated_data.extraction_date:
                job_defaults["extraction_date"] = validated_data.extraction_date
            
            # Create or find job post using validated data
            job, _ = JobPost.first_or_create(
                title=validated_data.title,
                company_id=company.id,
                defaults=job_defaults,
            )
            print(f"job post id: {job.id}")
            
            # Link scrape to job post
            scrape.job_post_id = job.id
            scrape.save()
            
            print(f"Successfully processed job: {validated_data.title} at {validated_data.company_name}")
            
        except Exception as e:
            print(f"Error processing validated evaluation: {e}")
            print(f"Validated data: {validated_data.dict()}")
            raise

    def analyze_html_with_ai(self, scrape: Scrape) -> ParsedJobData:
        # Determine content to analyze - use job_content if HTML is too large
        max_html_size = 50000  # Adjust this threshold as needed
        content_to_analyze = scrape.html or ""
        
        if len(content_to_analyze) > max_html_size and scrape.job_content:
            print(f"HTML too large ({len(content_to_analyze)} chars), using job_content instead")
            content_to_analyze = scrape.job_content
        
        # Create a direct prompt for the agent
        prompt = f"""
Extract job posting information from the following content and return structured data.

Please extract:
- Job title
- Company name  
- Company display name (if different from name)
- Job description
- Posted date (if available)
- Extraction date (current date/time)

Content to analyze:
{content_to_analyze}
"""

        write_prompt_to_file(
            prompt,
            kind="job_parser",
            identifiers={
                "scrape_id": scrape.id,
                "job_post_id": getattr(scrape, "job_post_id", None),
            },
        )

        # Get or create the agent
        if self.agent is None:
            self.agent = self.get_agent()

        try:
            # Use the Pydantic AI agent to get structured output
            result = self.agent.run_sync(prompt)
            return result.output
            
        except Exception as e:
            print(f"Error analyzing with AI agent: {e}")
            raise
