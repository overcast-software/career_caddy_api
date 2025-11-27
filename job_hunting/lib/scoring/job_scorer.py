import json
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, field_validator


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
            data.get("explanation")
            or data.get("evaluation")
            or data.get("explination")
        )
        return cls(score=score, explanation=str(explanation or "").strip())


class JobScorer:
    def __init__(self, client):
        self.client = client
        self.env = Environment(loader=FileSystemLoader("templates"))

    def score_job_match(self, job_description, resume):
        template = self.env.get_template("job_scorer_prompt.j2")
        prompt = template.render(job_description=job_description, resume=resume)
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
            )
            raw = (response.choices[0].message.content or "").strip()

            # Try strict JSON parsing; tolerate fenced code blocks
            text = raw
            if text.startswith("```"):
                parts = text.split("```")
                if len(parts) >= 2:
                    text = parts[1]
                    if text.lstrip().startswith("json"):
                        text = text.split("\n", 1)[1] if "\n" in text else ""

            data = json.loads(text)
            score_obj = Score.from_mapping(data)
            return {"score": score_obj.score, "explanation": score_obj.explanation}
        except Exception as e:
            print(f"Error scoring job match: {e}")
            return {"score": None, "explanation": f"Error: {e}"}
