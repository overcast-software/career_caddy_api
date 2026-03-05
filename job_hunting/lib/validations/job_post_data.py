from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class JobPostData(BaseModel):
    """Validation model for job posting data."""
    
    title: str = Field(..., description="Job title")
    description: str = Field(..., description="Job description")
    company_name: Optional[str] = Field(None, description="Company name")
    location: Optional[str] = Field(None, description="Job location")
    posted_date: Optional[datetime] = Field(None, description="Date the job was posted")
    extraction_date: Optional[datetime] = Field(None, description="Date the job data was extracted")
    link: Optional[str] = Field(None, description="URL link to the job posting")
    salary: Optional[str] = Field(None, description="Salary information")
    
    class Config:
        json_schema_extra = {
            "example": {
                "title": "Senior Software Engineer",
                "description": "We are looking for an experienced software engineer...",
                "company_name": "Tech Corp",
                "location": "San Francisco, CA",
                "posted_date": "2026-03-01T00:00:00",
                "extraction_date": "2026-03-05T00:00:00",
                "link": "https://example.com/jobs/12345",
                "salary": "$120,000 - $180,000"
            }
        }
