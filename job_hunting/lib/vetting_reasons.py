VETTING_REASONS: list[tuple[str, str]] = [
    ("compensation", "Compensation"),
    ("location", "Location / remote"),
    ("seniority", "Seniority mismatch"),
    ("stack", "Tech / stack mismatch"),
    ("company", "Dislike company"),
    ("other", "Other"),
]

VETTING_REASON_CODES: frozenset[str] = frozenset(code for code, _ in VETTING_REASONS)
