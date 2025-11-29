import json
import re
from typing import Union
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, field_validator
from .schemas import JobMatchRequest, JobMatchResponse


class Score(BaseModel):
    score: int
    explanation: str

    @field_validator("score")
    @classmethod
    def _validate_score(cls, v: int) -> int:
        if not 1 <= int(v) <= 100:
            raise ValueError("score must be between 1 and 100")
        return int(v)

    @classmethod
    def from_mapping(cls, data):
        # Accept common keys including legacy 'evaluation'/'explination'
        if not isinstance(data, dict):
            raise ValueError("Score.from_mapping expects a dict")
        score = data.get("score")
        explanation = (
            data.get("explanation") or data.get("evaluation") or data.get("explination")
        )
        return cls(score=score, explanation=str(explanation or "").strip())


class JobScorer:
    def __init__(self, client):
        self.client = client
        self.env = Environment(loader=FileSystemLoader("templates"))

    def _extract_json(self, text: str) -> dict:
        """Extract JSON from text, handling code fences if present."""
        # Remove markdown code fences if present
        text = re.sub(r"```(?:json)?\s*", "", text)
        text = text.strip()

        # Find first { and last } to extract JSON object
        start = text.find("{")
        end = text.rfind("}")

        if start == -1 or end == -1 or start >= end:
            raise ValueError("No valid JSON object found")

        json_str = text[start : end + 1]
        return json.loads(json_str)

    def _parse_fallback(self, text: str) -> dict:
        """Fallback parser for legacy Score:/Evaluation: format."""
        score_match = re.search(r"Score:\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
        eval_match = re.search(
            r"Evaluation:\s*(.*?)(?:\n|$)", text, re.IGNORECASE | re.DOTALL
        )

        if not score_match:
            raise ValueError("Could not extract score from text")

        score = int(float(score_match.group(1)))
        evaluation = (
            eval_match.group(1).strip() if eval_match else "No evaluation provided"
        )

        return {"score": score, "evaluation": evaluation}

    def score_job_match(
        self, job_description: Union[str, JobMatchRequest], resume: str = None
    ) -> JobMatchResponse:
        # Handle input validation
        if isinstance(job_description, str):
            if resume is None:
                raise ValueError(
                    "resume parameter required when job_description is a string"
                )
            request = JobMatchRequest(job_description=job_description, resume=resume)
        else:
            request = job_description

        template = self.env.get_template("job_scorer_prompt.j2")
        prompt = template.render(
            job_description=request.job_description, resume=request.resume
        )

        # Prepare request parameters
        request_params = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "max_tokens": 1000,
        }

        # Add JSON mode if supported
        try:
            request_params["response_format"] = {"type": "json_object"}
        except:
            # JSON mode not supported, continue without it
            pass

        response = self.client.chat.completions.create(**request_params)
        content = response.choices[0].message.content.strip()

        # Try to parse response
        try:
            # First try JSON parsing
            data = self._extract_json(content)
            return JobMatchResponse(**data)
        except (json.JSONDecodeError, ValueError):
            try:
                # Fallback to regex parsing
                data = self._parse_fallback(content)
                return JobMatchResponse(**data)
            except ValueError:
                raise ValueError(
                    f"Unable to parse job match response. Raw content: {content[:200]}..."
                )
