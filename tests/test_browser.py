"""End-to-end autofill against real HTML, driven by a real browser.

Skipped unless Playwright and a browser binary are actually available, so the
rest of the suite still runs anywhere. These are the tests that catch what unit
tests can't: label resolution depends on the DOM around a field, and getting it
wrong is how an agent ends up typing your name into "School name" — or refusing
to fill anything at all.

Run them with a non-default Chromium via JOB_APPLIER_BROWSER_PATH.
"""

import pytest

from app.apply import answers as answers_mod
from app.apply import browser, render

pytest.importorskip("playwright", reason="Playwright is an optional dependency")


@pytest.fixture(scope="module")
def chromium_available():
    from playwright.sync_api import sync_playwright

    from app import config

    try:
        with sync_playwright() as playwright:
            launch = {"headless": True}
            if config.BROWSER_PATH:
                launch["executable_path"] = config.BROWSER_PATH
            playwright.chromium.launch(**launch).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"No usable Chromium: {exc}")
    return True


PROFILE = {
    "name": "Alex Q. Doe",
    "email": "alex@example.com",
    "phone": "+1 512 555 0100",
    "location": "Austin, TX",
    "links": ["https://github.com/alexdoe", "https://www.linkedin.com/in/alexdoe"],
    "experience": [{"company": "Acme", "title": "Senior Backend Engineer"}],
}

# Inputs sit directly inside <form>, with no per-field wrapper. This is the
# layout that breaks naive label extraction: walking up to the parent element
# hands back the whole form's text, so every field inherits every other field's
# label — and the salary question poisons all of them.
FLAT_FORM = """
<!doctype html><html><body><h1>Apply</h1>
<form>
  <label for="fn">First Name *</label><input id="fn" name="first_name" required />
  <label for="ln">Last Name *</label><input id="ln" name="last_name" required />
  <label for="em">Email *</label><input id="em" name="email" type="email" required />
  <label for="ph">Phone</label><input id="ph" name="phone" />
  <label for="li">LinkedIn Profile</label><input id="li" name="urls[LinkedIn]" />
  <label for="school">School Name</label><input id="school" name="school" />
  <label for="res">Resume/CV *</label><input id="res" name="resume" type="file" required />
  <label for="sal">Desired Salary *</label><input id="sal" name="salary" required />
  <label for="auth">Are you legally authorized to work in the US? *</label>
  <select id="auth" name="work_auth" required><option value="">Select</option><option>Yes</option></select>
  <label for="gender">Gender</label>
  <select id="gender" name="gender"><option value="">Decline</option><option>Female</option></select>
  <input type="hidden" name="token" value="x" />
  <button type="submit">Submit Application</button>
</form></body></html>
"""

ANSWERABLE_FORM = """
<!doctype html><html><body><h1>Apply</h1>
<form id="f">
  <div><label for="n">Full Name *</label><input id="n" required /></div>
  <div><label for="e">Email *</label><input id="e" type="email" required /></div>
  <div><label for="r">Resume *</label><input id="r" type="file" required /></div>
  <div><button type="button">Next</button><button type="submit">Submit Application</button></div>
</form>
<script>document.getElementById('f').addEventListener('submit', (event) => {
  event.preventDefault();
  document.body.innerHTML = '<h1 id="done">Application received</h1>';
});</script>
</body></html>
"""


@pytest.fixture()
def values(tmp_path):
    resume = render.markdown_to_pdf("# Alex Q. Doe\n\nBackend engineer.\n", tmp_path / "r.pdf")
    return answers_mod.build(PROFILE, resume_path=str(resume),
                             cover_letter_text="Dear hiring team, ...")


def _page(tmp_path, html, name="form.html"):
    path = tmp_path / name
    path.write_text(html, encoding="utf-8")
    return f"file://{path}"


def _by_label(items):
    return {item["label"]: item for item in items}


def test_flat_form_fills_what_it_should(chromium_available, tmp_path, values):
    result = browser.apply_to(_page(tmp_path, FLAT_FORM), values, headless=True)
    assert result.status == "needs_review"

    filled = " | ".join(f"{item['label']}={item['value']}" for item in result.filled)
    assert "Alex" in filled and "alex@example.com" in filled
    assert "linkedin.com/in/alexdoe" in filled
    assert "r.pdf" in filled  # the resume really was attached
    # Six labels, six correct values — not one field poisoned by its neighbours.
    assert len(result.filled) == 6


def test_flat_form_refuses_the_fields_it_must_not_answer(chromium_available, tmp_path, values):
    result = browser.apply_to(_page(tmp_path, FLAT_FORM), values, headless=True)
    reasons = " ".join(item["reason"] for item in result.unfilled)
    labels = " ".join(item["label"] for item in result.unfilled)

    assert "Desired Salary" in labels and "negotiable term" in reasons
    assert "Gender" in labels and "demographic" in reasons
    assert "legally authorized" in labels and "legal / eligibility" in reasons
    # The candidate's name must never land in someone else's name field.
    assert "School Name" in labels
    assert "Alex" not in " ".join(
        item["value"] for item in result.filled if "School" in item["label"])


def test_required_flags_come_from_the_field_not_the_form(chromium_available, tmp_path, values):
    """Phone is optional here; if the whole form's text leaks into each label,
    every field picks up somebody else's asterisk and looks required."""
    result = browser.apply_to(_page(tmp_path, FLAT_FORM), values, headless=True)
    unfilled = _by_label(result.unfilled)
    gender = next(item for label, item in unfilled.items() if "Gender" in label)
    assert gender["required"] is False


def test_auto_submit_is_held_back_by_unanswered_required_fields(
        chromium_available, tmp_path, values):
    result = browser.apply_to(_page(tmp_path, FLAT_FORM), values,
                              auto_submit=True, headless=True)
    assert result.status == "needs_review"
    assert "held back" in result.detail
    assert {item["label"].split(" ")[0] for item in result.blocking} >= {"Desired"}


def test_auto_submit_sends_a_fully_answerable_form(chromium_available, tmp_path, values):
    result = browser.apply_to(_page(tmp_path, ANSWERABLE_FORM, "simple.html"), values,
                              auto_submit=True, headless=True)
    assert result.status == "submitted"
    assert result.unfilled == []
    # "Next" must never be mistaken for "Submit".
    assert "Submit Application" in result.detail


def test_without_auto_submit_nothing_is_sent(chromium_available, tmp_path, values):
    result = browser.apply_to(_page(tmp_path, ANSWERABLE_FORM, "simple.html"), values,
                              headless=True)
    assert result.status == "needs_review"
    assert "Not submitted" in result.detail


def test_screenshot_is_captured(chromium_available, tmp_path, values):
    shot = tmp_path / "shot.png"
    result = browser.apply_to(_page(tmp_path, ANSWERABLE_FORM, "simple.html"), values,
                              headless=True, screenshot_path=shot)
    assert result.screenshot == str(shot)
    assert shot.read_bytes().startswith(b"\x89PNG")


def test_an_unreachable_page_fails_without_raising(chromium_available, tmp_path, values):
    result = browser.apply_to("file:///nonexistent/nowhere.html", values, headless=True)
    assert result.status == "failed"
    assert result.detail
