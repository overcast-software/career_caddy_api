import os
from typing import Union
from jinja2 import Environment, FileSystemLoader
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIChatModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.providers.ollama import OllamaProvider
from .schemas import JobMatchRequest, JobMatchResponse
from job_hunting.lib.services.prompt_utils import write_prompt_to_file


class JobScorer:
    def __init__(self, client=None, agent=None):
        self.client = client
        self.agent = agent
        self.env = Environment(loader=FileSystemLoader("templates"))

    def get_agent(self) -> Agent:
        if self.agent:
            return self.agent
        # Prefer OpenAI if OPENAI_API_KEY is set; otherwise fall back to local Ollama.
        try:
            if os.getenv("OPENAI_API_KEY"):
                openai_model = OpenAIResponsesModel("gpt-5")
                self.agent = Agent(openai_model, output_type=JobMatchResponse)
                return self.agent
        except Exception:
            # Fall back to Ollama if OpenAI model initialization fails for any reason
            pass

        ollama_model = OpenAIChatModel(
            model_name="qwen3-coder",
            provider=OllamaProvider(base_url="http://localhost:11434/v1"),
        )
        self.agent = Agent(ollama_model, output_type=JobMatchResponse)
        return self.agent


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

        write_prompt_to_file(
            prompt,
            kind="job_scorer",
            identifiers={},
        )

        if self.agent is None:
            self.agent = self.get_agent()

        try:
            result = self.agent.run_sync(prompt)
        except Exception as e:
            raise ValueError(f"Scoring failed: {e}")

        if not hasattr(result, "output") or result.output is None:
            raise ValueError("Scoring failed: no structured output returned")

        # Return the Pydantic model directly (no JSON/dict)
        return result.output
