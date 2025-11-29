import re
from pydantic import BaseModel, field_validator
from pydantic.types import constr, conint


class JobMatchRequest(BaseModel):
    job_description: constr(min_length=1, strip_whitespace=True)
    resume: constr(min_length=1, strip_whitespace=True)


class JobMatchResponse(BaseModel):
    score: conint(ge=0, le=100)
    evaluation: constr(min_length=1, strip_whitespace=True)

    @field_validator("score", mode="before")
    @classmethod
    def coerce_score(cls, v):
        if isinstance(v, int):
            return max(0, min(100, v))
        
        if isinstance(v, float):
            return max(0, min(100, int(v)))
        
        if isinstance(v, str):
            # Handle patterns like "Score: 85" or "85"
            match = re.search(r'(\d+(?:\.\d+)?)', v)
            if match:
                score = float(match.group(1))
                return max(0, min(100, int(score)))
        
        raise ValueError(f"Cannot coerce score value: {v}")
