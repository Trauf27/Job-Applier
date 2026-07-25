"""LLM-backed document generation: tailored resumes, cover letters, deep review.

Ground rule enforced in every system prompt: the model may reorder, re-weight,
and rephrase what is already in the profile, but it may never invent employers,
titles, dates, credentials, or metrics. A resume that overstates your history is
worse than no resume.
"""

from __future__ import annotations

import json
from typing import Any

from . import llm

TRUTHFULNESS_RULE = (
    "Absolute rule: use ONLY facts present in the candidate profile. Never invent or "
    "embellish employers, job titles, dates, degrees, certifications, metrics, or "
    "technologies the candidate did not list. You may reorder, re-emphasise, and "
    "rewrite existing facts to speak to the job description, and you may use the "
    "job's own vocabulary for things the candidate has genuinely done. If the job "
    "requires something the candidate lacks, leave it out of the document and note "
    "it separately as a gap."
)

RESUME_SYSTEM = f"""You are an experienced technical recruiter and resume writer.
You rewrite a candidate's existing resume so it speaks directly to one specific job.

{TRUTHFULNESS_RULE}

Output format: GitHub-flavoured Markdown, ready to paste into a document.
Structure: name and contact line, a 2-3 line summary aimed at this role, a skills
line ordered by relevance to the job, then experience in reverse-chronological
order with 3-5 achievement bullets each, then education and certifications.
Bullets start with a strong verb, state scope, and end with an outcome where the
profile supplies one. Keep it to roughly one page for under 10 years of
experience, two pages otherwise. Do not add commentary before or after the
resume."""

COVER_LETTER_SYSTEM = f"""You are writing a short, specific cover letter on the
candidate's behalf.

{TRUTHFULNESS_RULE}

Constraints: 220-320 words. Three or four short paragraphs. Open with the
specific role and one concrete reason this candidate fits it — no "I am writing
to apply for". Middle paragraphs connect two or three real accomplishments from
the profile to explicit needs in the job description. Close with a plain,
confident line about next steps. Plain professional English. No em dashes, no
buzzword stacking, no flattery about the company being "innovative". Output the
letter body as Markdown with no subject line and no commentary."""

REVIEW_SYSTEM = """You are a blunt, experienced hiring advisor. You assess how well
one candidate matches one job posting and you do not inflate scores. A 90+ means
the candidate would very likely clear a recruiter screen; a 50 means it is a
stretch worth a single tailored attempt; below 30 means don't bother. Judge only
on evidence in the profile."""

FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "description": "0-100 likelihood this candidate clears a recruiter screen",
        },
        "verdict": {
            "type": "string",
            "enum": ["strong match", "worth applying", "stretch", "skip"],
        },
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete overlaps between profile and posting",
        },
        "gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Requirements the profile does not evidence",
        },
        "talking_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Things to lead with in the resume and cover letter",
        },
        "resume_advice": {
            "type": "string",
            "description": "What to change on the resume for this specific job",
        },
    },
    "required": ["score", "verdict", "strengths", "gaps", "talking_points", "resume_advice"],
    "additionalProperties": False,
}


def _profile_block(profile: dict[str, Any]) -> str:
    """Serialise the profile compactly, dropping empty fields to save tokens."""
    trimmed = {k: v for k, v in profile.items() if v not in (None, "", [], {})}
    trimmed.pop("updated_at", None)
    return json.dumps(trimmed, indent=2, ensure_ascii=False)


def _job_block(job: dict[str, Any], *, description_limit: int = 12000) -> str:
    description = (job.get("description") or "").strip()
    if len(description) > description_limit:
        description = description[:description_limit] + "\n[description truncated]"
    return (
        f"Company: {job.get('company')}\n"
        f"Title: {job.get('title')}\n"
        f"Location: {job.get('location') or 'unspecified'}"
        f"{' (remote)' if job.get('remote') else ''}\n"
        f"URL: {job.get('url')}\n\n"
        f"--- JOB DESCRIPTION ---\n{description or '(no description available)'}"
    )


def tailored_resume(profile: dict[str, Any], job: dict[str, Any]) -> str:
    prompt = (
        "Rewrite this candidate's resume for the job below.\n\n"
        f"=== CANDIDATE PROFILE (JSON) ===\n{_profile_block(profile)}\n\n"
        f"=== TARGET JOB ===\n{_job_block(job)}\n\n"
        "Return only the tailored resume in Markdown."
    )
    return llm.complete(RESUME_SYSTEM, prompt, max_tokens=16000, effort="high")


def cover_letter(profile: dict[str, Any], job: dict[str, Any]) -> str:
    prompt = (
        "Write a cover letter for this candidate and job.\n\n"
        f"=== CANDIDATE PROFILE (JSON) ===\n{_profile_block(profile)}\n\n"
        f"=== TARGET JOB ===\n{_job_block(job)}\n\n"
        "Return only the letter body in Markdown."
    )
    return llm.complete(COVER_LETTER_SYSTEM, prompt, max_tokens=4000, effort="medium")


def deep_review(profile: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Second-pass LLM fit assessment for a job that already passed local scoring."""
    prompt = (
        "Assess this candidate against this job.\n\n"
        f"=== CANDIDATE PROFILE (JSON) ===\n{_profile_block(profile)}\n\n"
        f"=== TARGET JOB ===\n{_job_block(job, description_limit=8000)}"
    )
    return llm.complete_json(REVIEW_SYSTEM, prompt, FIT_SCHEMA, max_tokens=4000, effort="medium")


def parse_resume(resume_text: str) -> dict[str, Any]:
    """Turn a pasted resume into a structured profile the user can then edit."""
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "email": {"type": "string"},
            "phone": {"type": "string"},
            "location": {"type": "string"},
            "headline": {"type": "string"},
            "summary": {"type": "string"},
            "links": {"type": "array", "items": {"type": "string"}},
            "skills": {"type": "array", "items": {"type": "string"}},
            "seniority": {"type": "string"},
            "experience": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "company": {"type": "string"},
                        "title": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "location": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["company", "title", "start", "end", "location", "bullets"],
                    "additionalProperties": False,
                },
            },
            "education": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "school": {"type": "string"},
                        "degree": {"type": "string"},
                        "year": {"type": "string"},
                    },
                    "required": ["school", "degree", "year"],
                    "additionalProperties": False,
                },
            },
            "certifications": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "name", "email", "phone", "location", "headline", "summary", "links",
            "skills", "seniority", "experience", "education", "certifications",
        ],
        "additionalProperties": False,
    }
    system = (
        "You extract structured data from resumes. Transcribe only what is written; "
        "never infer or invent. Use an empty string for anything absent. For "
        "`seniority`, pick one of: intern, junior, mid, senior, staff, principal, "
        "manager — based on the most recent role."
    )
    return llm.complete_json(
        system,
        f"Extract this resume into the schema.\n\n=== RESUME ===\n{resume_text[:40000]}",
        schema,
        max_tokens=8000,
        effort="medium",
    )
