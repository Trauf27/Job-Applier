"""Form answers, the never-answer rules, and the PDF renderer."""

import pytest

from app.apply import answers as answers_mod
from app.apply import browser, render

PROFILE = {
    "name": "Alex Q. Doe",
    "email": "alex@example.com",
    "phone": "+1 512 555 0100",
    "location": "Austin, TX",
    "headline": "Senior Backend Engineer",
    "summary": "Ten years on payments infrastructure.",
    "links": [
        "https://github.com/alexdoe",
        "https://www.linkedin.com/in/alexdoe",
        "https://alexdoe.dev",
    ],
    "experience": [{"company": "Acme", "title": "Senior Backend Engineer",
                    "start": "2020", "end": "Present", "bullets": ["Built billing"]}],
}


@pytest.fixture()
def answers():
    return answers_mod.build(PROFILE, resume_path="/tmp/resume.pdf",
                            cover_letter_text="Dear team, ...")


def test_build_splits_name_and_sorts_links(answers):
    assert answers.get("first_name").value == "Alex"
    assert answers.get("last_name").value == "Q. Doe"
    assert answers.get("linkedin").value == "https://www.linkedin.com/in/alexdoe"
    assert answers.get("github").value == "https://github.com/alexdoe"
    assert answers.get("company").value == "Acme"


def test_build_omits_fields_the_profile_lacks():
    sparse = answers_mod.build({"name": "Sam", "email": "sam@example.com"})
    assert sparse.get("phone") is None
    assert sparse.get("linkedin") is None
    # A one-word name yields no surname rather than a guessed one.
    assert sparse.get("last_name") is None


@pytest.mark.parametrize(
    "label,expected",
    [
        ("First name *", "first_name"),
        ("Last Name", "last_name"),
        ("Full name", "full_name"),
        ("Email address", "email"),          # must not fall through to `location`
        ("Mobile phone number", "phone"),
        ("LinkedIn Profile URL", "linkedin"),
        ("GitHub", "github"),
        ("Personal website", "website"),
        ("Current company name", "company"),  # must not fall through to `full_name`
        ("Current job title", "headline"),
        ("City", "location"),
        ("Resume/CV", "resume"),
        ("Cover letter", "cover_letter"),
    ],
)
def test_labels_route_to_the_right_answer(label, expected, answers):
    key, reason = answers_mod.match_field(label, answers)
    assert key == expected, reason


@pytest.mark.parametrize(
    "label",
    [
        "Gender", "Race / Ethnicity", "Are you Hispanic or Latino?",
        "Veteran status", "Do you have a disability?",
        "Will you now or in the future require sponsorship?",
        "Are you legally authorized to work in the United States?",
        "Do you hold an active security clearance?",
        "Desired salary", "Expected compensation", "Earliest start date",
        "Are you willing to relocate?", "How did you hear about us?",
    ],
)
def test_loaded_questions_are_never_answered(label, answers):
    key, reason = answers_mod.match_field(label, answers)
    assert key is None
    assert reason


def test_someone_elses_name_is_not_filled_in(answers):
    for label in ("School name", "Reference name", "Emergency contact name",
                  "Hiring manager name"):
        key, reason = answers_mod.match_field(label, answers)
        assert key is None, f"{label} should not receive the candidate's name"
        assert "someone else" in reason


def test_unknown_and_unanswerable_fields_report_why(answers):
    key, reason = answers_mod.match_field("What is your favourite algorithm?", answers)
    assert key is None and "unrecognised" in reason

    thin = answers_mod.build({"name": "Sam"})
    key, reason = answers_mod.match_field("Phone number", thin)
    assert key is None and "nothing in the profile" in reason


def test_resume_and_cover_letter_need_the_file_to_exist():
    without = answers_mod.build(PROFILE)
    assert answers_mod.match_field("Resume", without) == (None, "nothing in the profile answers 'Resume'")

    with_file = answers_mod.build(PROFILE, resume_path="/tmp/resume.pdf")
    assert answers_mod.match_field("Resume", with_file)[0] == "resume"


def test_blank_label_is_skipped(answers):
    assert answers_mod.match_field("", answers) == (None, "no label")


# --- PDF rendering ------------------------------------------------------------

RESUME_MD = """# Alex Q. Doe
alex@example.com · Austin, TX · [GitHub](https://github.com/alexdoe)

## Summary
Ten years on payments infrastructure — **billing**, ledgers, and reconciliation.

## Experience

### Acme — Senior Backend Engineer
- Rebuilt the billing service, cutting invoice errors by 80%
- Led the migration to PostgreSQL 15 across 40 services

## Education
- BS Computer Science, UT Austin
"""


def test_pdf_is_structurally_valid(tmp_path):
    path = render.markdown_to_pdf(RESUME_MD, tmp_path / "resume.pdf")
    data = path.read_bytes()

    assert data.startswith(b"%PDF-1.4")
    assert data.rstrip().endswith(b"%%EOF")
    # A reader needs the xref offset to open the file at all.
    assert b"startxref" in data and b"/Type /Catalog" in data
    assert data.count(b" obj\n") == data.count(b"endobj\n")


def test_pdf_keeps_the_text_selectable(tmp_path):
    path = render.markdown_to_pdf(RESUME_MD, tmp_path / "resume.pdf")
    data = path.read_bytes()
    # ATS parsers read the text, so it has to survive into the content stream.
    assert b"(Alex Q. Doe) Tj" in data
    assert b"Senior Backend Engineer" in data
    # Markdown emphasis must not leak through as literal asterisks.
    assert b"**billing**" not in data


def test_pdf_paginates_long_resumes(tmp_path):
    long_resume = "# Alex Doe\n\n" + "\n".join(
        f"- Achievement {i} with enough words in it to wrap onto a second line neatly"
        for i in range(120)
    )
    data = render.markdown_to_pdf(long_resume, tmp_path / "long.pdf").read_bytes()
    assert data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") > 1


def test_pdf_escapes_parentheses(tmp_path):
    """Unescaped parens terminate a PDF string early and corrupt the file."""
    data = render.markdown_to_pdf("Worked at Acme (formerly Ajax)", tmp_path / "p.pdf").read_bytes()
    assert rb"\(formerly Ajax\)" in data


def test_text_fallback(tmp_path):
    text = render.markdown_to_text(RESUME_MD, tmp_path / "resume.txt").read_text()
    assert "Alex Q. Doe" in text
    assert "**" not in text


# --- driver availability ------------------------------------------------------


def test_missing_playwright_degrades_to_a_clear_message(monkeypatch):
    monkeypatch.setattr(browser, "available", lambda: False)
    result = browser.apply_to("https://example.com/apply", answers_mod.build(PROFILE))
    assert result.status == "skipped"
    assert "playwright" in result.detail.lower()


def test_blocking_counts_only_required_fields():
    result = browser.AttemptResult(status="needs_review", unfilled=[
        {"label": "Gender", "reason": "demographic", "required": False},
        {"label": "Work authorization", "reason": "legal", "required": True},
    ])
    assert [item["label"] for item in result.blocking] == ["Work authorization"]
