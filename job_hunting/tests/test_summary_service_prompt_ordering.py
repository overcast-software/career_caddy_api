"""Regression: the summary template used to put injected_prompt under a
trailing ``Additional Instructions:`` block, AFTER the leading hard
constraint ``Keep the summary to less than 80 words.`` Strong leading
directives beat trailing ones — mirrors the cover-letter and
answer-builder fixes (PR #163, ``application_prompt_builder.py:215-225``).
This test renders the template directly via the same Jinja env the
service uses (``SummaryService.__init__``) and asserts the new ordering.
"""

from jinja2 import Environment, FileSystemLoader

from django.test import TestCase


class TestSummaryServicePromptOrdering(TestCase):
    """The user-supplied injected_prompt must precede the leading constraints."""

    def setUp(self):
        # Same env shape as SummaryService.__init__ — render directly to
        # keep this off the AI client and DB. The summary_job task
        # boundary verifies threading; this test verifies the template
        # shape only.
        self.env = Environment(
            loader=FileSystemLoader("templates"),
            autoescape=False,  # nosec B701 - text/LLM prompt templates
        )
        self.template = self.env.get_template("summary_service_prompt.j2")

    def _render(self, injected_prompt=None):
        return self.template.render(
            job_description="Build distributed systems.",
            resume="# Resume\n\nFive years of backend.",
            injected_prompt=injected_prompt,
        )

    def test_injected_prompt_appears_before_constraints(self):
        prompt = self._render(injected_prompt="START WITH THE WORD UNICORN")
        injected_idx = prompt.find("START WITH THE WORD UNICORN")
        constraints_idx = prompt.find("You are a career counselor")
        self.assertNotEqual(
            injected_idx, -1, "injected prompt missing from rendered template"
        )
        self.assertNotEqual(
            constraints_idx,
            -1,
            "leading system message missing from rendered template",
        )
        self.assertLess(
            injected_idx,
            constraints_idx,
            "injected prompt must precede the leading system message so the "
            "model treats it as the controlling directive",
        )

    def test_injected_prompt_flagged_as_priority_override(self):
        prompt = self._render(injected_prompt="be terse")
        self.assertIn("## User Instructions (PRIORITY", prompt)

    def test_no_injected_prompt_omits_override_section(self):
        prompt = self._render(injected_prompt=None)
        self.assertNotIn("User Instructions (PRIORITY", prompt)
        self.assertIn("You are a career counselor", prompt)
