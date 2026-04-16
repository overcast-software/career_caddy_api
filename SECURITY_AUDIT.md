# Security Audit: Multi-Tenancy Data Isolation

**Date:** 2026-04-15
**Scope:** All ViewSets and function views in `job_hunting/api/views.py`
**Branch:** report-only — no code changes

---

## Executive Summary

The API has **no systematic row-level data isolation**. ViewSets that were built later (Resume, Summary, Score, CoverLetter, JobApplication, Question, Answer, ApiKey, AiUsage, Scrape) each implement their own user filtering. But `BaseViewSet`'s default `list()`, `retrieve()`, and `destroy()` methods have **no user scoping**, and several ViewSets inherit these defaults without overriding them.

Any authenticated user can currently:
- **Read** all educations, certifications, experiences, descriptions, and projects belonging to every user
- **Delete** any of those records by ID
- **Retrieve** any user's resumes, scores, cover letters, applications, and summaries via `/api/v1/users/{id}/resumes` (and similar)
- **Export** any user's full career data via `/api/v1/career-data/{user_id}/`

**Affected models:** Education, Certification, Experience, Description, Project
**Affected actions:** 5 detail actions on DjangoUserViewSet, `career_data()` function view

---

## Endpoint Matrix

| ViewSet | list | retrieve | create | update | destroy | custom actions | Status |
|---------|------|----------|--------|--------|---------|----------------|--------|
| ResumeViewSet | scoped | scoped | scoped | scoped | scoped | scoped | **SECURE** |
| SummaryViewSet | scoped | scoped | scoped | scoped | scoped | — | **SECURE** |
| ScoreViewSet | scoped | scoped | scoped | scoped | scoped | — | **SECURE** |
| CoverLetterViewSet | scoped | scoped | scoped | scoped | scoped | — | **SECURE** |
| JobApplicationViewSet | scoped | scoped | scoped | scoped | scoped | scoped | **SECURE** |
| QuestionViewSet | scoped | scoped | scoped | scoped | scoped | scoped | **SECURE** |
| AnswerViewSet | scoped | scoped | scoped | scoped | scoped | — | **SECURE** |
| ApiKeyViewSet | scoped | scoped | scoped | scoped | — | — | **SECURE** |
| AiUsageViewSet | scoped | — | scoped | — | — | scoped | **SECURE** |
| ScrapeViewSet | scoped | scoped | scoped | unscoped | unscoped | — | **PARTIAL** |
| DjangoUserViewSet | scoped | scoped | — | — | scoped | **5 UNSCOPED** | **VULNERABLE** |
| JobPostViewSet | scoped* | scoped* | scoped | scoped | scoped | — | **SECURE*** |
| EducationViewSet | **UNSCOPED** | **UNSCOPED** | unscoped | unscoped | **UNSCOPED** | — | **CRITICAL** |
| CertificationViewSet | **UNSCOPED** | **UNSCOPED** | unscoped | unscoped | **UNSCOPED** | — | **CRITICAL** |
| ExperienceViewSet | **UNSCOPED** | **UNSCOPED** | unscoped | unscoped | — | unscoped | **CRITICAL** |
| DescriptionViewSet | **UNSCOPED** | **UNSCOPED** | unscoped | unscoped | **UNSCOPED** | — | **CRITICAL** |
| ProjectViewSet | **UNSCOPED** | **UNSCOPED** | unscoped | unscoped | — | unscoped | **CRITICAL** |
| CompanyViewSet | all | all | dedup | — | — | — | **BY DESIGN** |
| StatusViewSet | all | — | — | — | — | — | **BY DESIGN** |
| InvitationViewSet | admin | admin | admin | — | admin | — | **SECURE** |

\* JobPostViewSet uses intentional OR logic: creator OR has application OR has score.

---

## Vulnerability Details

### CRITICAL-1: BaseViewSet defaults have no user scoping

**File:** `views.py`

**`list()` (lines 947–956):**
```python
def list(self, request):
    items = list(self.model.objects.all())  # returns ALL records
```

**`retrieve()` (lines 964–973):**
```python
def retrieve(self, request, pk=None):
    obj = self._get_obj(pk)  # model.objects.filter(pk=pk).first() — no user check
```

**`destroy()` (lines 1074–1076):**
```python
def destroy(self, request, pk=None):
    self.model.objects.filter(pk=int(pk)).delete()  # deletes any record by ID
```

**Impact:** Any ViewSet that doesn't override these methods exposes all records to all authenticated users, including deletion.

**Affected ViewSets:** EducationViewSet, CertificationViewSet, ExperienceViewSet, DescriptionViewSet, ProjectViewSet.

---

### CRITICAL-2: Resume child models have no user FK

Education, Certification, Experience, and Description have no direct `user` or `created_by` field. They connect to users only through Resume via join tables (ResumeEducation, ResumeCertification, ResumeExperience, ExperienceDescription / ProjectDescription).

**Attack scenario:**
```
GET /api/v1/educations/          → all educations across all users
GET /api/v1/certifications/42    → any certification by ID
DELETE /api/v1/descriptions/150  → delete another user's bullet point
```

**EducationViewSet.destroy() (line 6370–6372):**
```python
def destroy(self, request, pk=None):
    Education.objects.filter(pk=int(pk)).delete()  # no ownership check
```

Identical pattern at CertificationViewSet (6479–6481) and DescriptionViewSet (6598–6600).

---

### CRITICAL-3: ProjectViewSet inherits unscoped defaults

Project **has** a `user` FK (line 7007–7009) but the ViewSet provides no `list()`, `retrieve()`, or `destroy()` override — it inherits `BaseViewSet.list()` which returns `model.objects.all()`.

**Attack scenario:**
```
GET /api/v1/projects/  → returns all users' projects
```

---

### CRITICAL-4: career_data() accepts arbitrary user_id

**File:** `views.py` (lines 7058–7073)

```python
def career_data(request, user_id=None):
    target_user_id = user_id if user_id is not None else request.user.id
    if user_id is not None and user_id != request.user.id:
        pass  # "For now, allow access" — explicit no-op
    career_data = CareerData.for_user(target_user_id)
```

**Attack scenario:**
```
GET /api/v1/users/99/career-data/  → full career export for user 99
```

---

### HIGH-1: DjangoUserViewSet detail actions leak cross-user data

Five `@action(detail=True)` endpoints on DjangoUserViewSet accept any user ID and return that user's records without authorization:

| Action | Lines | Endpoint |
|--------|-------|----------|
| `resumes()` | 1917–1927 | `GET /api/v1/users/{id}/resumes` |
| `scores()` | 1936–1946 | `GET /api/v1/users/{id}/scores` |
| `cover_letters()` | 1955–1965 | `GET /api/v1/users/{id}/cover-letters` |
| `applications()` | 1974–1984 | `GET /api/v1/users/{id}/job-applications` |
| `summaries()` | 1993–2003 | `GET /api/v1/users/{id}/summaries` |

Note: `api_keys()` (2013–2028) is **safe** — it checks `request.user.is_staff or user.id == request.user.id` at line 2023.

**Attack scenario:**
```
GET /api/v1/users/1/resumes      → user 1's resumes
GET /api/v1/users/1/cover-letters → user 1's cover letters
# iterate user IDs 1..N to enumerate all data
```

---

### MEDIUM-1: _build_included() has incomplete type filtering

**File:** `views.py` (lines 878–890)

The sideloading filter only checks ownership for four resource types:
```python
if effective_type in ("cover-letter", "score", "summary", "job-application")
    and t_user_id is not None
    ...
```

Resources like education, certification, description, experience, and project are **not filtered** in includes. If a parent resource sideloads them via `?include=`, another user's child records could leak through.

---

### MEDIUM-2: ScrapeViewSet update/destroy not scoped

`ScrapeViewSet.list()` and `.retrieve()` are properly scoped. But `update()` and `partial_update()` call `super()` (BaseViewSet) which has no ownership check. `destroy()` is also inherited from BaseViewSet.

---

## By-Design Exceptions

| Resource | Rationale |
|----------|-----------|
| **Company** | Shared across all users — no `created_by` FK. Deduplicated on create by name. |
| **Status** | Global lookup table for application status values. |
| **Skill** | Global lookup table. |

---

## Recommended Remediation

### Phase 1: OwnedQuerysetMixin (from notes.org)

```python
class OwnedQuerysetMixin:
    user_lookup = "user_id"

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.is_staff:
            return qs
        return qs.filter(**{self.user_lookup: self.request.user.id})
```

Apply to each ViewSet with the appropriate `user_lookup`:

| ViewSet | user_lookup |
|---------|-------------|
| ProjectViewSet | `user_id` |
| EducationViewSet | `resumeeducation__resume__user_id` |
| CertificationViewSet | `resumecertification__resume__user_id` |
| ExperienceViewSet | `resumeexperience__resume__user_id` |
| DescriptionViewSet | `experiencedescription__experience__resumeexperience__resume__user_id` (or via project) |

Description is the hardest — it links through both Experience and Project. May need a custom `get_queryset()` with an OR across both paths.

### Phase 2: Fix detail actions on DjangoUserViewSet

Add ownership check to each action:
```python
if not request.user.is_staff and int(pk) != request.user.id:
    return Response({"errors": [{"detail": "Forbidden"}]}, status=403)
```

### Phase 3: Fix career_data()

Replace the `pass` with:
```python
if user_id is not None and user_id != request.user.id:
    if not request.user.is_staff:
        return Response({"errors": [{"detail": "Forbidden"}]}, status=403)
```

### Phase 4: Extend _build_included() filter

Add education, certification, description, experience, and project to the type whitelist, using resume-chain ownership verification.

### Phase 5: Add multi-tenancy isolation tests

Write tests that create two users, create data for each, and verify that user A cannot list/retrieve/update/delete user B's records across all endpoints.

---

## Priority Ordering

1. **DjangoUserViewSet detail actions** — easiest fix, highest immediate risk (direct ID enumeration)
2. **career_data()** — one-line fix, full data export vulnerability
3. **ProjectViewSet** — has user FK, just needs list/retrieve/destroy override
4. **Education / Certification / Experience / Description** — need join-table scoping
5. **_build_included() filter** — defense-in-depth for sideloaded data
6. **ScrapeViewSet update/destroy** — lower risk since list/retrieve are scoped
7. **Multi-tenancy test suite** — prevents regressions
