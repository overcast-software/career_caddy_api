"""CC-250 — the answer prompt must not demand specificity about an absent job.

Found by tracing an answer that was fluent, confident, and about a completely
different job. Two questions from the same user seven minutes apart: the one
carrying a job post talked about game development (the post was "Senior Roblox
Developer & Game Designer"); the one carrying `job_post = None` talked about
AWS event-driven systems, fraud prevention and low-latency pipelines, and said
"this role" three times. Not a vaguer answer — a confident answer to a job that
was never supplied.

The mechanism was entirely in the prompt. The default preamble told the model
to use "job details" it had not been given and to be "specific to the job" it
had never seen, while the `## Job Details` section was silently omitted. The
only material actually present was the career profile, so the model
manufactured specificity out of it and asserted it about the role. "Truthful"
and "specific to the job" cannot both be satisfied with no job post, and the
prompt never said which wins.

Two guards, and the second matters more than the first:

1. the demand for job-specificity disappears when there is no job post, and
2. the absence is stated OUT LOUD. An omitted section is invisible to the
   model; an explicit line is not.

Both directions are asserted here — the job-post path must keep saying
"specific to the job", or a fix that simply deleted the phrase everywhere
would pass while making every real answer worse.
"""

from django.test import TestCase
from django.contrib.auth import get_user_model

from job_hunting.lib.services.application_prompt_builder import (
    ApplicationPromptBuilder,
)
from job_hunting.models import Company, JobPost, Question

User = get_user_model()

# The literal line the builder must emit in place of "## Job Details". Kept as
# a constant so the assertions below quote the artifact rather than a
# paraphrase of it — this string is the fix.
ABSENCE_LINE = "No job post is linked to this question."


class TestPromptWithNoJobPost(TestCase):
    """`context["job_post"] is None` — the reported case."""

    def setUp(self):
        self.user = User.objects.create_user(username="cc250_none", password="pw")
        self.question = Question.objects.create(
            content="Why are you interested in this role?",
            created_by=self.user,
        )
        self.context = {
            "question": self.question,
            "job_post": None,
            "company": None,
            "resumes": [],
            "resume": None,
            "cover_letters": [],
            "qas": [],
            "user": self.user,
        }

    def test_preamble_does_not_demand_specificity_to_the_job(self):
        prompt = ApplicationPromptBuilder().build(self.context)

        self.assertNotIn("specific to the job", prompt)

    def test_preamble_does_not_claim_job_details_are_available(self):
        """The old preamble listed "job details" among the provided context.

        Naming a source that is not in the prompt is its own invitation to
        invent one, independently of the "specific to the job" clause.
        """
        prompt = ApplicationPromptBuilder().build(self.context)

        self.assertNotIn("job details, resume, Q&A history", prompt)

    def test_absence_is_stated_explicitly_under_a_job_details_heading(self):
        """The load-bearing half of the fix: say it, do not merely omit it."""
        prompt = ApplicationPromptBuilder().build(self.context)

        self.assertIn("## Job Details", prompt)
        self.assertIn(ABSENCE_LINE, prompt)
        self.assertIn("Do not characterise the", prompt)

    def test_preamble_itself_forbids_characterising_the_role(self):
        """The instruction, not only the context section, carries the rule.

        The preamble is the front matter the model reads first; leaving the
        constraint solely in a section two thousand tokens later repeats the
        original mistake at a different distance.
        """
        prompt = ApplicationPromptBuilder().build(self.context)
        # Split on the Job Details heading, not on "## Question to Answer" —
        # the preamble quotes that heading by name, so splitting there would
        # slice the preamble in half and the assertions would test nothing.
        preamble = prompt.split("## Job Details")[0]

        self.assertIn("No job post is linked to this question", preamble)
        self.assertIn("do NOT characterise the role", preamble)

    def test_output_format_contract_survives(self):
        """Rewriting the preamble must not drop the plain-text contract.

        The user pastes this output verbatim into an application form, so the
        format rules are not decoration on the instruction being changed.
        """
        prompt = ApplicationPromptBuilder().build(self.context)

        self.assertIn("OUTPUT FORMAT — strictly plain text", prompt)
        self.assertIn("Answer ONLY the question", prompt)

    def test_absence_line_is_emitted_even_with_caller_supplied_instructions(self):
        """`/generate-prompt/` lets the caller replace the preamble entirely.

        The caller owns the instruction; it does not own the context. A custom
        preamble must not silently take the explicit-absence guard with it.
        """
        prompt = ApplicationPromptBuilder().build(
            self.context,
            "Answer in the style of a cover letter.",
        )

        self.assertIn(ABSENCE_LINE, prompt)


class TestPromptWithJobPost(TestCase):
    """The job-post path is unchanged — the fix must be conditional, not a deletion."""

    def setUp(self):
        self.user = User.objects.create_user(username="cc250_some", password="pw")
        self.company = Company.objects.create(name="Blue Sky Interactive")
        self.job_post = JobPost.objects.create(
            title="Senior Roblox Developer & Game Designer",
            description="We are seeking an experienced Roblox Developer.",
            company=self.company,
        )
        self.question = Question.objects.create(
            content="Why are you interested in this role?",
            created_by=self.user,
        )
        self.context = {
            "question": self.question,
            "job_post": self.job_post,
            "company": self.company,
            "resumes": [],
            "resume": None,
            "cover_letters": [],
            "qas": [],
            "user": self.user,
        }

    def test_preamble_still_demands_specificity_to_the_job(self):
        prompt = ApplicationPromptBuilder().build(self.context)

        self.assertIn("specific to the job", prompt)
        self.assertIn("job details, resume, Q&A history", prompt)

    def test_real_job_details_are_emitted_and_the_absence_line_is_not(self):
        prompt = ApplicationPromptBuilder().build(self.context)

        self.assertIn("## Job Details", prompt)
        self.assertIn("Senior Roblox Developer & Game Designer", prompt)
        self.assertNotIn(ABSENCE_LINE, prompt)
