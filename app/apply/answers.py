"""Turning a profile into answers an application form will accept.

The browser driver knows how to click and type; this module knows *what* to
type. Keeping the two apart means the interesting half — which profile field
answers "Preferred first name" — is plain data and fully testable without a
browser.

Two rules shape everything here:

1. **Never invent an answer.** If the profile doesn't hold the fact, the field
   comes back unanswered and a human fills it in. A confidently wrong answer to
   "Are you legally authorized to work in the US?" is worse than a blank one.
2. **Never answer a demographic or legally-loaded question.** EEO race/gender/
   veteran/disability fields, sponsorship, and salary expectations are left for
   you, always, even with auto-submit on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Fields the agent will not touch under any setting. Matched against the field's
# visible label, name, and id.
NEVER_ANSWER = [
    (re.compile(r"gender|\bsex\b|race|ethnic|hispanic|latino|veteran|disability|"
                r"lgbt|sexual orientation|pronoun", re.I), "demographic / EEO question"),
    (re.compile(r"sponsor|visa|work authori[sz]|authori[sz]ed to work|right to work|"
                r"citizen|clearance|felony|convict|background check|"
                r"drug (?:test|screen)|\bwork auth\b|18 years", re.I),
     "legal / eligibility question"),
    (re.compile(r"salary|compensation|desired pay|rate expectation|notice period|"
                r"date available|start date|willing to relocate", re.I),
     "negotiable term — yours to decide"),
    (re.compile(r"how did you hear|referr?al|referred by", re.I), "attribution question"),
]


@dataclass
class Answer:
    """One value the driver may put into a form."""

    value: str
    source: str  # which profile field it came from, for the audit log


@dataclass
class ApplicationAnswers:
    """Everything the driver may use for one application."""

    values: dict[str, Answer] = field(default_factory=dict)
    resume_path: str | None = None
    cover_letter_path: str | None = None
    cover_letter_text: str = ""

    def get(self, key: str) -> Answer | None:
        return self.values.get(key)

    def as_dict(self) -> dict[str, str]:
        return {key: answer.value for key, answer in self.values.items()}


# Ordered most-specific first, because these patterns overlap by design:
# "Company name" has to reach the `company` rule before the `full_name` rule,
# and "Email address" has to reach `email` before `location`.
FIELD_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"e-?mail", re.I)),
    ("first_name", re.compile(r"\b(first|given|fore)\s*name\b|^fname$", re.I)),
    ("last_name", re.compile(r"\b(last|family|sur)\s*name\b|^lname$", re.I)),
    ("linkedin", re.compile(r"linked-?in", re.I)),
    ("github", re.compile(r"git-?hub", re.I)),
    ("resume", re.compile(r"resume|résumé|\bcv\b", re.I)),
    ("cover_letter", re.compile(r"cover letter|why (?:do you )?(?:are you |want)|"
                               r"motivation|additional information", re.I)),
    ("company", re.compile(r"current (?:employer|company)|company name|employer", re.I)),
    ("headline", re.compile(r"headline|current title|job title|occupation|"
                            r"current role|current position", re.I)),
    ("phone", re.compile(r"phone|mobile|telephone|contact number", re.I)),
    ("website", re.compile(r"website|portfolio|personal site|\burl\b|homepage", re.I)),
    ("location", re.compile(r"location|city|address|where.*(?:based|located)", re.I)),
    ("summary", re.compile(r"summary|about you|tell us about|introduce yourself", re.I)),
    ("full_name", re.compile(r"\b(?:full|legal|preferred|your)?\s*name\b|^name$", re.I)),
]

# `full_name`'s pattern is deliberately loose, so it needs a veto: these are all
# somebody else's name, not the candidate's.
NOT_THE_CANDIDATE = re.compile(
    r"school|university|college|institution|reference|manager|supervisor|"
    r"emergency|contact person|spouse|guardian|recruiter",
    re.I,
)


def _links(profile: dict[str, Any]) -> dict[str, str]:
    """Bucket the profile's links so a "LinkedIn URL" field gets the LinkedIn one."""
    buckets = {"linkedin": "", "github": "", "website": ""}
    for raw in profile.get("links") or []:
        link = str(raw).strip()
        if not link:
            continue
        lowered = link.lower()
        if "linkedin.com" in lowered and not buckets["linkedin"]:
            buckets["linkedin"] = link
        elif "github.com" in lowered and not buckets["github"]:
            buckets["github"] = link
        elif not buckets["website"]:
            buckets["website"] = link
    return buckets


def _split_name(full_name: str) -> tuple[str, str]:
    parts = [p for p in (full_name or "").split() if p]
    if len(parts) < 2:
        return (parts[0] if parts else ""), ""
    return parts[0], " ".join(parts[1:])


def build(
    profile: dict[str, Any],
    *,
    resume_path: str | None = None,
    cover_letter_path: str | None = None,
    cover_letter_text: str = "",
) -> ApplicationAnswers:
    """Project a profile onto the answer keys `FIELD_RULES` can resolve."""
    name = (profile.get("name") or "").strip()
    first, last = _split_name(name)
    links = _links(profile)
    experience = profile.get("experience") or []
    current = experience[0] if experience else {}

    raw: dict[str, tuple[str, str]] = {
        "full_name": (name, "profile.name"),
        "first_name": (first, "profile.name"),
        "last_name": (last, "profile.name"),
        "email": ((profile.get("email") or "").strip(), "profile.email"),
        "phone": ((profile.get("phone") or "").strip(), "profile.phone"),
        "location": ((profile.get("location") or "").strip(), "profile.location"),
        "linkedin": (links["linkedin"], "profile.links"),
        "github": (links["github"], "profile.links"),
        "website": (links["website"] or links["github"], "profile.links"),
        "headline": ((profile.get("headline") or "").strip(), "profile.headline"),
        "company": ((current.get("company") or "").strip(), "profile.experience[0]"),
        "summary": ((profile.get("summary") or "").strip(), "profile.summary"),
        "cover_letter": (cover_letter_text.strip(), "generated cover letter"),
    }

    return ApplicationAnswers(
        values={k: Answer(v, src) for k, (v, src) in raw.items() if v},
        resume_path=resume_path,
        cover_letter_path=cover_letter_path,
        cover_letter_text=cover_letter_text,
    )


def is_off_limits(label: str) -> str | None:
    """Return why a field must be left to the human, or None if it's fair game."""
    for pattern, reason in NEVER_ANSWER:
        if pattern.search(label or ""):
            return reason
    return None


def match_field(label: str, answers: ApplicationAnswers) -> tuple[str | None, str | None]:
    """Resolve one form field to an answer key.

    Returns `(key, skip_reason)`. Exactly one is ever set: a key when we have a
    value to type, a reason when the field is off-limits or unanswerable.
    """
    label = re.sub(r"\s+", " ", (label or "")).strip()
    if not label:
        return None, "no label"

    off_limits = is_off_limits(label)
    if off_limits:
        return None, off_limits

    for key, pattern in FIELD_RULES:
        if not pattern.search(label):
            continue
        if key in ("full_name", "first_name", "last_name") and NOT_THE_CANDIDATE.search(label):
            return None, f"'{label}' asks about someone else"
        if _have_value(key, answers):
            return key, None
        return None, f"nothing in the profile answers '{label}'"

    return None, f"unrecognised field '{label}'"


def _have_value(key: str, answers: ApplicationAnswers) -> bool:
    if key == "resume":
        return bool(answers.resume_path)
    if key == "cover_letter":
        return bool(answers.cover_letter_text or answers.cover_letter_path)
    return bool(answers.get(key))
