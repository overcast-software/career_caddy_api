import json

from django.conf import settings
from django.db import models
from .base import GetMixin


class SafeJSONField(models.JSONField):
    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            return json.loads(value)
        return value


class Profile(GetMixin, models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile_obj",
    )
    phone = models.CharField(max_length=50, null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_guest = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    linkedin = models.CharField(max_length=255, null=True, blank=True)
    github = models.CharField(max_length=255, null=True, blank=True)
    links = SafeJSONField(null=True, blank=True, default=dict)
    onboarding = SafeJSONField(null=True, blank=True, default=dict)
    auto_score = models.BooleanField(default=False)
    # BACK-91: publishing ingested job posts to the fediverse is a per-user
    # opt-in, default OFF. Ingestion is always private; when this is True the
    # ingestion/persist paths mark freshly-created posts public
    # (`audience=[AS2_PUBLIC]`) via `JobPost.audience_for_user`. There is no
    # per-post publish button — visibility follows this single setting.
    federate_posts = models.BooleanField(default=False)

    class Meta:
        db_table = "profile"

    def __str__(self):
        return f"Profile({self.user_id})"

    # Subjective keys are user-claimed state nothing in the DB can verify —
    # only AW (or the user via Settings) writes them. PATCH on the user
    # resource may set these.
    SUBJECTIVE_ONBOARDING_KEYS = frozenset({"wizard_enabled", "resume_reviewed"})

    # Derived keys are recomputed by reconcile from real records (Resume,
    # JobPost, Score, CoverLetter, profile basics on the User row). PATCH
    # writes to derived keys are rejected with 400 — the value is a function
    # of state, not user input.
    DERIVED_ONBOARDING_KEYS = frozenset(
        {
            "profile_basics",
            "resume_imported",
            "first_job_post",
            "first_score",
            "first_cover_letter",
        }
    )

    @staticmethod
    def default_onboarding():
        return {
            "wizard_enabled": True,
            "profile_basics": False,
            "resume_imported": False,
            "resume_reviewed": False,
            "first_job_post": False,
            "first_score": False,
            "first_cover_letter": False,
        }

    def resolved_onboarding(self):
        """Return stored onboarding merged on top of the default shape."""
        shape = self.default_onboarding()
        stored = self.onboarding if isinstance(self.onboarding, dict) else {}
        shape.update({k: v for k, v in stored.items() if k in shape})
        return shape

    @classmethod
    def split_onboarding(cls, blob: dict) -> dict:
        """Partition an onboarding blob into {derived, subjective} halves.

        The dedicated /api/v1/users/:id/onboarding/ + /reconcile/ endpoints return this
        shape so callers know which half they may PATCH (subjective) and
        which half is recomputed server-side (derived).
        """
        return {
            "derived": {
                k: bool(blob.get(k, False)) for k in cls.DERIVED_ONBOARDING_KEYS
            },
            "subjective": {
                k: bool(blob.get(k, k == "wizard_enabled"))
                for k in cls.SUBJECTIVE_ONBOARDING_KEYS
            },
        }

    def merge_onboarding(self, patch):
        """Merge a partial dict into onboarding (keys not in default shape are ignored).

        Permissive merge — accepts any default-shape key. Used internally by
        reconcile (which writes derived keys based on actual data). Callers
        coming from PATCH should use `merge_subjective_onboarding` instead so
        derived-key writes get rejected loudly.
        """
        if not isinstance(patch, dict):
            return
        allowed = self.default_onboarding().keys()
        current = self.onboarding if isinstance(self.onboarding, dict) else {}
        current = {**current}
        for k, v in patch.items():
            if k in allowed:
                current[k] = bool(v)
        self.onboarding = current

    def profile_basics_complete(self) -> bool:
        """True when the linked User has first_name, last_name, and email set.

        Single source of truth for the `profile_basics` derived flag; called
        from both `derive_onboarding_from_state` (used by reconcile) and the
        user serializer (which patches this at read time so fresh signups
        see it true immediately rather than waiting for the next reconcile).
        """
        user = self.user
        if user is None:
            return False
        return bool(user.first_name and user.last_name and user.email)

    def derive_onboarding_from_state(self) -> dict:
        """Recompute the derived half of onboarding from real records.

        Every derived key is a function of: the linked User's basic fields,
        and the existence of Resume / JobPost / Score / CoverLetter rows
        owned by that user. Subjective keys (`wizard_enabled`,
        `resume_reviewed`) are not touched here — the caller merges this
        result over the stored blob, preserving subjective values.
        """
        from job_hunting.models.resume import Resume
        from job_hunting.models.job_post import JobPost
        from job_hunting.models.score import Score
        from job_hunting.models.cover_letter import CoverLetter

        uid = self.user_id
        return {
            "profile_basics": self.profile_basics_complete(),
            "resume_imported": Resume.objects.filter(user_id=uid).exists(),
            "first_job_post": JobPost.objects.filter(created_by_id=uid).exists(),
            "first_score": Score.objects.filter(user_id=uid).exists(),
            "first_cover_letter": CoverLetter.objects.filter(user_id=uid).exists(),
        }

    def merge_subjective_onboarding(self, patch: dict) -> set:
        """Atomic-merge only subjective keys from `patch`. Returns the set
        of rejected (derived or unknown) keys.

        If any rejected keys appear, *no* keys from the patch are applied —
        the caller is expected to 400 the request. Atomic semantics mean a
        client never gets a partial write back when their patch is malformed.
        Empty set = clean merge.
        """
        if not isinstance(patch, dict):
            return set()
        rejected = {k for k in patch.keys() if k not in self.SUBJECTIVE_ONBOARDING_KEYS}
        if rejected:
            return rejected
        current = self.onboarding if isinstance(self.onboarding, dict) else {}
        current = {**current}
        for k, v in patch.items():
            current[k] = bool(v)
        self.onboarding = current
        return rejected
