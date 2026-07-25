"""Deterministic profile <-> job scoring.

This runs locally on every synced job — no API key, no cost — and produces a
0-100 score with an explanation. The LLM re-ranker (see `tailor.deep_review`) is
an optional second pass over the top of this list, not a replacement for it.

Weights are tuned so that a job can't score well on freshness alone: title fit
and skill overlap together carry 70 of the 100 points.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

WEIGHTS = {"title": 35, "skills": 35, "location": 20, "freshness": 10}

# Ordered low -> high. Used to measure the distance between the seniority the
# profile targets and the one the posting implies.
SENIORITY_LADDER = [
    ("intern", ["intern", "internship", "co-op"]),
    ("junior", ["junior", "jr.", "entry level", "entry-level", "associate", "graduate", " i "]),
    ("mid", ["mid", "mid-level", "intermediate", " ii "]),
    ("senior", ["senior", "sr.", "sr ", " iii "]),
    ("staff", ["staff", "lead", "principal engineer"]),
    ("principal", ["principal", "distinguished", "architect", "fellow"]),
    ("manager", ["manager", "head of", "director", "vp", "vice president", "chief"]),
]

_WORD_RE = re.compile(r"[a-z0-9#+.]+")
_STOPWORDS = {
    "a", "an", "and", "the", "of", "for", "to", "in", "on", "with", "at", "by",
    "or", "our", "we", "you", "your", "is", "are", "be", "as", "it", "that",
    "this", "will", "have", "has", "from", "job", "role", "team", "work",
}


def tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1}


def _phrase_present(phrase: str, haystack: str) -> bool:
    """Substring match on word boundaries, so 'go' doesn't match 'goal'."""
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", haystack) is not None


def detect_seniority(text: str) -> str | None:
    padded = f" {text.lower()} "
    found: list[tuple[int, str]] = []
    for index, (name, markers) in enumerate(SENIORITY_LADDER):
        if any(marker in padded for marker in markers):
            found.append((index, name))
    if not found:
        return None
    # Prefer the most senior signal present ("Senior Staff Engineer" -> staff).
    return max(found)[1]


def _ladder_index(name: str | None) -> int | None:
    if not name:
        return None
    for index, (label, _) in enumerate(SENIORITY_LADDER):
        if label == name:
            return index
    return None


def _score_title(job_title: str, targets: Iterable[str]) -> tuple[float, str]:
    targets = [t for t in targets if t and t.strip()]
    if not targets:
        return 0.6, "No target titles set — title scored neutrally"

    title_lower = job_title.lower()
    title_tokens = tokenize(job_title)
    best = 0.0
    best_target = ""
    for target in targets:
        if _phrase_present(target, title_lower):
            ratio = 1.0
        else:
            target_tokens = tokenize(target)
            if not target_tokens:
                continue
            ratio = len(target_tokens & title_tokens) / len(target_tokens)
        if ratio > best:
            best, best_target = ratio, target

    if best >= 1.0:
        return 1.0, f"Title matches target '{best_target}'"
    if best > 0:
        return best, f"Partial title match with '{best_target}' ({int(best * 100)}%)"
    return 0.0, "Title does not match any target title"


def _score_skills(text: str, skills: Iterable[str]) -> tuple[float, list[str], list[str], str]:
    skills = [s for s in skills if s and s.strip()]
    if not skills:
        return 0.5, [], [], "No skills on profile — skills scored neutrally"

    haystack = text.lower()
    matched = [s for s in skills if _phrase_present(s, haystack)]
    missing = [s for s in skills if s not in matched]
    ratio = len(matched) / len(skills)
    # A posting rarely name-checks every skill you have; treat 60% as a full
    # match so strong-but-not-exhaustive overlap isn't punished.
    normalized = min(1.0, ratio / 0.6)
    return (
        normalized,
        matched,
        missing,
        f"{len(matched)}/{len(skills)} profile skills appear in the posting",
    )


def _score_location(job: dict[str, Any], profile: dict[str, Any]) -> tuple[float, str]:
    targets = [t for t in profile.get("target_locations") or [] if t and t.strip()]
    remote_ok = bool(profile.get("remote_ok", True))
    job_location = (job.get("location") or "").lower()
    is_remote = bool(job.get("remote"))

    if is_remote and remote_ok:
        return 1.0, "Remote role and you're open to remote"
    if not targets:
        return 0.6 if not is_remote else 0.8, "No target locations set"
    for target in targets:
        if _phrase_present(target, job_location):
            return 1.0, f"Location matches '{target}'"
    if is_remote:
        return 0.7, "Remote role, but you listed on-site preferences"
    return 0.1, f"Location '{job.get('location') or 'unknown'}' is outside your targets"


def _score_freshness(posted_at: str | None) -> tuple[float, str]:
    if not posted_at:
        return 0.5, "Posting date unknown"
    try:
        posted = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.5, "Posting date unparseable"
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - posted).days
    if days <= 3:
        return 1.0, f"Posted {days} day(s) ago"
    if days <= 14:
        return 0.8, f"Posted {days} days ago"
    if days <= 30:
        return 0.5, f"Posted {days} days ago"
    if days <= 60:
        return 0.25, f"Posted {days} days ago"
    return 0.0, f"Posted {days} days ago — likely stale"


def score_job(job: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Return {"score": float 0-100, "reasons": [...], "matched_skills": [...], ...}."""
    title = job.get("title") or ""
    description = job.get("description") or ""
    combined = f"{title}\n{description}"

    title_ratio, title_reason = _score_title(title, profile.get("target_titles") or [])
    skill_ratio, matched, missing, skill_reason = _score_skills(
        combined, profile.get("skills") or []
    )
    location_ratio, location_reason = _score_location(job, profile)
    fresh_ratio, fresh_reason = _score_freshness(job.get("posted_at"))

    score = (
        title_ratio * WEIGHTS["title"]
        + skill_ratio * WEIGHTS["skills"]
        + location_ratio * WEIGHTS["location"]
        + fresh_ratio * WEIGHTS["freshness"]
    )

    reasons = [title_reason, skill_reason, location_reason, fresh_reason]
    penalties: list[str] = []

    # Seniority distance: one rung off is a nudge, two or more is a real mismatch.
    wanted = _ladder_index((profile.get("seniority") or "").strip().lower() or None)
    actual = _ladder_index(detect_seniority(title))
    if wanted is not None and actual is not None and wanted != actual:
        distance = abs(wanted - actual)
        penalty = min(20.0, distance * 8.0)
        score -= penalty
        penalties.append(
            f"Seniority mismatch: posting reads {SENIORITY_LADDER[actual][0]}, "
            f"you target {SENIORITY_LADDER[wanted][0]} (-{penalty:.0f})"
        )

    haystack = combined.lower()
    for keyword in profile.get("exclude_keywords") or []:
        if _phrase_present(keyword, haystack):
            score -= 25
            penalties.append(f"Contains excluded keyword '{keyword}' (-25)")

    score = max(0.0, min(100.0, score))
    return {
        "score": round(score, 1),
        "reasons": reasons + penalties,
        "matched_skills": matched,
        "missing_skills": missing,
        "components": {
            "title": round(title_ratio * WEIGHTS["title"], 1),
            "skills": round(skill_ratio * WEIGHTS["skills"], 1),
            "location": round(location_ratio * WEIGHTS["location"], 1),
            "freshness": round(fresh_ratio * WEIGHTS["freshness"], 1),
        },
    }
