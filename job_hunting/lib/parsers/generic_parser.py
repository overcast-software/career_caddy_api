from job_hunting.lib.models import Scrape, JobPost, Company
import sys
import json
from datetime import datetime, date
from typing import Optional
from pydantic import BaseModel, Field, validator
from jinja2 import Environment, FileSystemLoader
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
    def __init__(self, client):
        self.client = client
        # Set up Jinja2 environment
        self.env = Environment(loader=FileSystemLoader("templates"))

    def parse(self, scrape: Scrape):
        job_description = self.analyze_html_with_ai(scrape)
        try:
            raw_evaluation = json.loads(job_description)
        except json.JSONDecodeError as e:
            print(f"Failed to parse AI response as JSON: {e}")
            print(f"Raw response: {job_description}")
            raise ValueError(f"Invalid JSON response from AI: {e}")
        
        # Validate the parsed data using Pydantic
        try:
            validated_data = ParsedJobData(**raw_evaluation)
        except Exception as e:
            print(f"Data validation failed: {e}")
            print(f"Raw data: {raw_evaluation}")
            raise ValueError(f"Invalid job data structure: {e}")
        
        self.process_evaluation(scrape, validated_data)

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

    def analyze_html_with_ai(self, scrape: Scrape) -> str:
        # Determine content to analyze - use job_content if HTML is too large
        max_html_size = 50000  # Adjust this threshold as needed
        content_to_analyze = scrape.html or ""
        
        if len(content_to_analyze) > max_html_size and scrape.job_content:
            print(f"HTML too large ({len(content_to_analyze)} chars), using job_content instead")
            content_to_analyze = scrape.job_content
        
        # Load and render the template
        template = self.env.get_template("job_parser_prompt.j2")
        prompt = template.render(html_content=content_to_analyze)

        write_prompt_to_file(
            prompt,
            kind="job_parser",
            identifiers={
                "scrape_id": scrape.id,
                "job_post_id": getattr(scrape, "job_post_id", None),
            },
        )

        # Create structured output schema for the AI
        schema = {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Job title",
                    "minLength": 1,
                    "maxLength": 500
                },
                "company_name": {
                    "type": "string", 
                    "description": "Company name",
                    "minLength": 1,
                    "maxLength": 200
                },
                "company_display_name": {
                    "type": ["string", "null"],
                    "description": "Company display name (optional)",
                    "maxLength": 200
                },
                "description": {
                    "type": ["string", "null"],
                    "description": "Job description (optional)"
                },
                "posted_date": {
                    "type": ["string", "null"],
                    "description": "Job posting date in ISO format (optional)"
                },
                "extraction_date": {
                    "type": ["string", "null"], 
                    "description": "Data extraction date in ISO format (optional)"
                }
            },
            "required": ["title", "company_name"],
            "additionalProperties": False
        }

        messages = [
            {
                "role": "system",
                "content": "You are a bot that evaluates content of job posts to extract relevant data. Return only valid JSON that matches the required schema.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                max_tokens=2000,
                response_format={"type": "json_object"},
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "extract_job_data",
                        "description": "Extract structured job posting data",
                        "parameters": schema
                    }
                }],
                tool_choice={"type": "function", "function": {"name": "extract_job_data"}}
            )
            
            # Extract the function call result
            if response.choices[0].message.tool_calls:
                tool_call = response.choices[0].message.tool_calls[0]
                if tool_call.function.name == "extract_job_data":
                    return tool_call.function.arguments
            
            # Fallback to regular content if no tool calls
            return response.choices[0].message.content.strip()
            
        except Exception as e:
            print(f"Error analyzing with ChatGPT: {e}")
            raise
