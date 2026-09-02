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

Four guards, in the order a reader meets them:

1. the demand for job-specificity disappears when there is no job post,
2. the absence is stated OUT LOUD. An omitted section is invisible to the
   model; an explicit line is not,
3. the guard forbids characterising the ROLE, and forbids characterising the
   COMPANY only when no company is in the prompt either. `Question.company`
   is its own nullable FK, so "no job post" does not imply "no company" —
   telling the model to ignore a "## Company" section it can plainly see is
   the original bug pointed the other way, and
4. the guard is repeated inside the trailing "the instructions win" block.
   That block hands the last word to the user's directive over everything
   above it, and `AnswerService.generate_answer` always passes one, so
   without the carve-out a directive like "emphasise why I'm a strong fit for
   this role" ends the prompt by cancelling the fix.

Both directions are asserted for each — the job-post path must keep saying
"specific to the job", the company-less path must keep forbidding the company,
and the job-post + directive path must keep its reminder unchanged. A fix that
simply deleted a phrase everywhere would pass a one-sided test while making
every real answer worse.
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
ROLE_GUARD = "Do NOT characterise the role"
# The clause that must appear ONLY when the prompt carries no company either.
COMPANY_CLAUSE = "the company, or the fit between them"


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
        self.assertIn(ROLE_GUARD, prompt)

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

        self.assertIn(ABSENCE_LINE, preamble)
        self.assertIn(ROLE_GUARD, preamble)

    def test_output_format_contract_survives(self):
        """Rewriting the preamble must not drop the plain-text contract.

        The user pastes this output verbatim into an application form, so the
        format rules are not decoration on the instruction being changed.
        """
        prompt = ApplicationPromptBuilder().build(self.context)

        self.assertIn("OUTPUT FORMAT — strictly plain text", prompt)
        self.assertIn("Answer ONLY the question", prompt)

    def test_company_is_forbidden_when_no_company_is_in_the_prompt_either(self):
        """With neither job post nor company, the guard covers both.

        The other half of the conditional in
        TestPromptWithCompanyButNoJobPost — if the company clause were simply
        deleted rather than made conditional, that test would still pass and
        this one would catch it.
        """
        prompt = ApplicationPromptBuilder().build(self.context)

        self.assertIn(COMPANY_CLAUSE, prompt)
        self.assertNotIn("## Company", prompt)

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

    def test_trailing_instructions_win_block_does_not_cancel_the_guard(self):
        """The production path, and the one that nearly undid the fix.

        `AnswerService.generate_answer` always calls `build(...,
        injected_prompt=clean_injected, ...)`, and the trailing block tells the
        model that everything above it is reference material the instructions
        outrank. The no-job-post guard is above it. A user directive asking for
        role-specificity would therefore be the last word, and CC-250 would
        recur through the block designed to make directives stick.
        """
        directive = "emphasise why I'm a strong fit for this role"

        prompt = ApplicationPromptBuilder().build(
            self.context, injected_prompt=directive
        )

        reminder = prompt.split("## Reminder")[1]
        self.assertIn(ABSENCE_LINE, reminder)
        self.assertIn("not something an instruction can supply", reminder)
        # The carve-out must not cost the block its whole point: the directive
        # is still the last thing in the prompt.
        self.assertTrue(prompt.rstrip().endswith(directive))


class TestPromptWithCompanyButNoJobPost(TestCase):
    """`job_post is None` but `company` is set — a supported, distinct state.

    `Question.company` is its own nullable FK and `load_context_for_question`
    resolves it from `question.company_id` before ever consulting the job
    post's, so this is not a hypothetical arrangement of the context dict; it
    is what a company-scoped question loads as. `build()` emits "## Company"
    from `context["company"]` with no reference to the job post, so a blanket
    "do not characterise the company" tells the model to ignore a populated
    section printed a few lines below the instruction that forbids it.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="cc250_co", password="pw")
        self.company = Company.objects.create(
            name="Acme Robotics", notes="Series B, 40 engineers, ships weekly."
        )
        self.question = Question.objects.create(
            content="Why do you want to work at Acme?",
            company=self.company,
            created_by=self.user,
        )
        self.context = {
            "question": self.question,
            "job_post": None,
            "company": self.company,
            "resumes": [],
            "resume": None,
            "cover_letters": [],
            "qas": [],
            "user": self.user,
        }

    def test_company_section_is_populated(self):
        """The premise. If this ever stops holding the rest is moot."""
        prompt = ApplicationPromptBuilder().build(self.context)

        self.assertIn("## Company", prompt)
        self.assertIn("Acme Robotics", prompt)
        self.assertIn("Series B, 40 engineers", prompt)

    def test_the_company_is_not_forbidden_while_its_notes_are_printed(self):
        prompt = ApplicationPromptBuilder().build(self.context)

        self.assertNotIn(COMPANY_CLAUSE, prompt)
        self.assertIn("The company IS", prompt)

    def test_the_role_is_still_forbidden(self):
        """Narrowing the guard must not blunt it — the role is still unknown."""
        prompt = ApplicationPromptBuilder().build(self.context)

        self.assertIn(ABSENCE_LINE, prompt)
        self.assertIn(ROLE_GUARD, prompt)
        self.assertNotIn("specific to the job", prompt)

    def test_preamble_lists_the_company_among_the_available_context(self):
        """Symmetry with the original bug.

        Naming a source the prompt does not carry invites invention; omitting
        one it does carry invites the model to leave it unused.
        """
        preamble = ApplicationPromptBuilder().build(self.context).split(
            "## Job Details"
        )[0]

        self.assertIn("company, resume, Q&A history", preamble)


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

    def test_trailing_reminder_keeps_its_unqualified_wording(self):
        """The carve-out is conditional too.

        With a job post there is nothing for a directive to invent, so the
        "instructions win" block must keep saying exactly that — an exception
        bolted on unconditionally would weaken every real answer's directive.
        """
        prompt = ApplicationPromptBuilder().build(
            self.context, injected_prompt="lead with the Roblox work"
        )

        reminder = prompt.split("## Reminder")[1]
        self.assertIn("the instructions win. Restated", reminder)
        self.assertNotIn(ABSENCE_LINE, reminder)
