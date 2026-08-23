"""CC-240 — the matcher refuses to contradict itself, and refuses weak picks.

The live failure this encodes: on 2026-08-12 a **Block** application page was
matched to a **Golden Analytics** job post at 0.9 confidence. The model's own
rationale admitted a different company and a different title, and justified the
pick because both were "hosted on the same platform link" — i.e. both on
Rippling.

The system prompt already forbade exactly that. It was ignored, so the rule is
enforced rather than requested — and enforced through pydantic-ai's own
mechanism (an ``output_validator`` raising ``ModelRetry``) rather than a
hand-rolled post-check. The model reports which company the PAGE is for; a pick
at a different company is a contradiction in its own output, detectable without
re-reading the page.

An earlier version of this guard scanned the page for the company using a
legal-suffix table and a token-length rule. That was bespoke — a pile of
conditionals that would have grown one branch per surprise — and it is gone.
"""

from unittest.mock import patch

from django.test import TestCase

from job_hunting.lib.parsers.job_matcher import (
    MATCH_CONFIDENCE_FLOOR,
    CandidatePost,
    JobMatcher,
    MatchDecision,
    _same_company,
)


# The real page context from the incident, abridged.
BLOCK_CONTEXT = dict(
    url="https://ats.rippling.com/block/jobs/abc123/apply?step=application",
    referrer="https://jobright.ai/jobs/info/deadbeef",
    page_title="Principal Security Engineer",
    text_excerpt=(
        "Block's purpose is economic empowerment. Briefly tell us about what "
        "that purpose means to you."
    ),
)

GOLDEN = CandidatePost(
    id="bbYlW9slpu",
    title="Security Engineer",
    company="Golden Analytics",
    link_host="jobright.ai",
)

BLOCK_POST = CandidatePost(
    id="blockpost01",
    title="Principal Security Engineer",
    company="Block",
    link_host="jobright.ai",
)


def _decision(job_post_id, confidence, company="Block", rationale="because"):
    return MatchDecision(
        job_post_id=job_post_id,
        application_company=company,
        confidence=confidence,
        rationale=rationale,
    )


class TestSameCompany(TestCase):
    """Small on purpose — it compares two NAMES, not a name against a page."""

    def test_the_incident_pair_is_not_the_same_company(self):
        self.assertFalse(_same_company("Golden Analytics", "Block"))

    def test_legal_suffixes_do_not_break_equality(self):
        self.assertTrue(_same_company("Block, Inc.", "Block"))
        self.assertTrue(_same_company("Block", "block inc"))

    def test_case_and_punctuation_are_ignored(self):
        self.assertTrue(_same_company("GOLDEN ANALYTICS", "Golden-Analytics"))

    def test_an_empty_side_cannot_contradict(self):
        """No stated company is not evidence of a mismatch."""
        self.assertTrue(_same_company("", "Block"))
        self.assertTrue(_same_company("Block", ""))

    def test_known_limitation_aliases_are_not_resolved(self):
        """Documented, not fixed: aliasing belongs to a Company lookup."""
        self.assertFalse(_same_company("Square", "Block"))


class TestConfidenceFloor(TestCase):
    """The one deterministic policy left. Deliberately not a ModelRetry —
    asking a model to be more confident just teaches it to inflate."""

    def _match(self, decision, candidates):
        m = JobMatcher(model="openai:gpt-5")
        with patch.object(JobMatcher, "_call_llm", return_value=decision):
            return m.match(candidates=candidates, **BLOCK_CONTEXT)

    def test_a_weak_pick_becomes_null(self):
        out = self._match(
            _decision(BLOCK_POST.id, MATCH_CONFIDENCE_FLOOR - 0.01), [BLOCK_POST]
        )
        self.assertIsNone(out.job_post_id)
        self.assertIn("confidence floor", out.rationale.lower())

    def test_a_confident_pick_survives(self):
        out = self._match(_decision(BLOCK_POST.id, 0.9), [BLOCK_POST])
        self.assertEqual(out.job_post_id, BLOCK_POST.id)
        self.assertEqual(out.confidence, 0.9)

    def test_confidence_is_preserved_on_a_dropped_pick(self):
        """Callers need to log how sure the model was about what we discarded."""
        weak = MATCH_CONFIDENCE_FLOOR - 0.2
        out = self._match(_decision(BLOCK_POST.id, weak), [BLOCK_POST])
        self.assertIsNone(out.job_post_id)
        self.assertAlmostEqual(out.confidence, weak)

    def test_null_passes_through_untouched(self):
        out = self._match(_decision(None, 0.1, rationale="nothing fits"), [GOLDEN])
        self.assertIsNone(out.job_post_id)
        self.assertEqual(out.rationale, "nothing fits")

    def test_the_reported_company_survives_a_drop(self):
        out = self._match(_decision(BLOCK_POST.id, 0.1), [BLOCK_POST])
        self.assertEqual(out.application_company, "Block")


class TestCrossCompanyValidator(TestCase):
    """The incident itself. The validator lives inside _call_llm's agent, so
    these exercise the predicate it is built from — a full agent run would
    need a live model."""

    def test_the_incident_is_a_contradiction(self):
        """Page reported as Block, pick at Golden Analytics -> must not stand."""
        self.assertFalse(_same_company(GOLDEN.company, "Block"))

    def test_a_correct_pick_is_not_a_contradiction(self):
        self.assertTrue(_same_company(BLOCK_POST.company, "Block"))

    def test_a_candidate_with_no_company_is_not_contradicted(self):
        anon = CandidatePost(id="anon1", title="Security Engineer", company="")
        self.assertTrue(_same_company(anon.company, "Block"))
