"""The part that actually fills in an application form.

Playwright drives a real browser: it opens the posting's apply page, reads every
field's visible label, asks `answers.match_field` what belongs in it, types what
it knows, uploads the tailored resume, and screenshots the result.

**It stops at the submit button.** The attempt comes back as `needs_review` with
the browser left open, listing what it filled and — more importantly — what it
refused to fill and why. You look, you fix the gaps, you press submit. Auto
submit exists (`auto_submit`), it is off by default, and it refuses to fire when
anything required is unanswered, because a half-filled application sent under
your name is worse than no application.

Playwright is an optional dependency. Without it every entry point returns a
`skipped` attempt explaining how to install it, and the rest of the app is
unaffected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from . import answers as answers_mod

# Buttons that move a multi-step form forward without committing anything.
_NEXT_RE = re.compile(r"^(next|continue|save and continue|proceed)$", re.I)
_SUBMIT_RE = re.compile(r"submit|send application|apply now|^apply$", re.I)

DEFAULT_TIMEOUT_MS = 20_000


@dataclass
class AttemptResult:
    """What one pass at a form achieved. Mirrors the `apply_attempts` table."""

    status: str  # prepared | needs_review | submitted | failed | skipped
    form_url: str = ""
    filled: list[dict[str, str]] = field(default_factory=list)
    unfilled: list[dict[str, str]] = field(default_factory=list)
    detail: str = ""
    screenshot: str | None = None

    @property
    def blocking(self) -> list[dict[str, str]]:
        """Unanswered fields the form marks required — the reason we can't submit."""
        return [item for item in self.unfilled if item.get("required")]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "form_url": self.form_url,
            "filled": self.filled,
            "unfilled": self.unfilled,
            "detail": self.detail,
            "screenshot": self.screenshot,
        }


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def unavailable_result() -> AttemptResult:
    return AttemptResult(
        status="skipped",
        detail=(
            "Playwright is not installed, so the form can't be driven. Run "
            "`pip install playwright && playwright install chromium` to enable "
            "auto-fill. Everything else — matching, tailoring, tracking — works "
            "without it."
        ),
    )


# Walks up at most a few levels looking for the smallest ancestor that wraps
# exactly this one control. Without the single-control test, a form whose inputs
# sit directly inside <form> hands back the entire form's text as every field's
# label — which makes every field look like it asks about salary, and every
# field look required.
_NEAREST_LABEL_JS = """
el => {
  const wrapping = el.closest('label');
  if (wrapping && wrapping.innerText.trim()) return wrapping.innerText;
  let node = el.parentElement;
  for (let depth = 0; depth < 3 && node; depth++) {
    if (node.querySelectorAll('input, textarea, select').length === 1) {
      const text = (node.innerText || '').trim();
      if (text && text.length <= 160) return text;
    }
    node = node.parentElement;
  }
  return '';
}
"""


def _label_text(page, element) -> str:
    """Everything a human would read as this field's name.

    Forms disagree about where the label lives — a `<label for>`, an aria-label,
    a placeholder, a wrapping div — so all of it is gathered and the matcher
    (and, importantly, the off-limits check) sees the union. What must *not*
    happen is text from neighbouring fields leaking in; see `_NEAREST_LABEL_JS`.
    """
    parts: list[str] = []

    element_id = element.get_attribute("id")
    if element_id:
        try:
            # CSS.escape isn't available here, so ids with quotes are skipped
            # rather than allowed to build a broken selector.
            if '"' not in element_id:
                label = page.query_selector(f'label[for="{element_id}"]')
                if label:
                    parts.append(label.inner_text())
        except Exception:  # noqa: BLE001 - a bad selector must not abort the pass
            pass

    if not parts:
        try:
            nearest = element.evaluate(_NEAREST_LABEL_JS)
            if nearest:
                parts.append(nearest)
        except Exception:  # noqa: BLE001
            pass

    for attribute in ("aria-label", "placeholder", "name", "id", "data-qa"):
        value = element.get_attribute(attribute)
        if value:
            parts.append(re.sub(r"[_\-\[\]]+", " ", value))

    return re.sub(r"\s+", " ", " ".join(parts)).strip()[:200]


def _is_required(element, label: str) -> bool:
    """Required per the markup, or per the asterisk convention in its own label."""
    if element.get_attribute("required") is not None:
        return True
    if (element.get_attribute("aria-required") or "").lower() == "true":
        return True
    return bool(re.search(r"\*|\brequired\b", label, re.I))


def _fill_form(page, values: answers_mod.ApplicationAnswers, result: AttemptResult) -> None:
    """One pass over every visible input on the current page."""
    elements = page.query_selector_all("input, textarea, select")
    for element in elements:
        try:
            input_type = (element.get_attribute("type") or "text").lower()
            if input_type in ("hidden", "submit", "button", "image", "reset"):
                continue
            if input_type != "file" and not element.is_visible():
                continue

            label = _label_text(page, element)
            required = _is_required(element, label)
            key, reason = answers_mod.match_field(label, values)

            if input_type == "file":
                path = values.resume_path
                if re.search(r"cover", label, re.I) and values.cover_letter_path:
                    path = values.cover_letter_path
                if path and Path(path).is_file():
                    element.set_input_files(path)
                    result.filled.append({"label": label[:120], "value": Path(path).name,
                                          "source": "generated document"})
                else:
                    result.unfilled.append({"label": label[:120], "reason": "no file to upload",
                                            "required": required})
                continue

            tag = (element.evaluate("el => el.tagName") or "").lower()
            if tag == "select":
                # Dropdowns are almost always the questions we refuse to answer
                # (country, sponsorship, EEO). Leave every one of them.
                result.unfilled.append({
                    "label": label[:120],
                    "reason": reason or "dropdown — pick this one yourself",
                    "required": required,
                })
                continue

            if input_type in ("checkbox", "radio"):
                result.unfilled.append({
                    "label": label[:120],
                    "reason": reason or "consent or choice — yours to make",
                    "required": required,
                })
                continue

            if key is None:
                if label:
                    result.unfilled.append({"label": label[:120],
                                            "reason": reason or "no match",
                                            "required": required})
                continue

            if key == "cover_letter":
                text = values.cover_letter_text
                if not text:
                    result.unfilled.append({"label": label[:120],
                                            "reason": "no cover letter generated",
                                            "required": required})
                    continue
                element.fill(text)
                result.filled.append({"label": label[:120], "value": f"{len(text)} chars",
                                      "source": "generated cover letter"})
                continue

            answer = values.get(key)
            if answer is None:
                continue
            element.fill(answer.value)
            result.filled.append({"label": label[:120], "value": answer.value[:80],
                                  "source": answer.source})
        except Exception as exc:  # noqa: BLE001 - one hostile field can't kill the pass
            result.unfilled.append({"label": "(unreadable field)", "reason": str(exc)[:160],
                                    "required": False})


def _open_apply_form(page, url: str) -> None:
    """Land on the form itself. Boards commonly gate it behind an Apply button."""
    page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    if page.query_selector("input[type=file], input[name*=email i], input[type=email]"):
        return
    for selector in ("text=/^apply/i", "a[href*='application']", "button:has-text('Apply')"):
        try:
            button = page.query_selector(selector)
            if button and button.is_visible():
                button.click(timeout=5_000)
                page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
                return
        except Exception:  # noqa: BLE001
            continue


def apply_to(
    url: str,
    values: answers_mod.ApplicationAnswers,
    *,
    auto_submit: bool = False,
    headless: bool = False,
    screenshot_path: str | Path | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> AttemptResult:
    """Fill the application form at `url`.

    Returns without submitting unless `auto_submit` is set *and* nothing
    required was left unanswered.
    """
    if not available():
        return unavailable_result()

    from playwright.sync_api import sync_playwright

    result = AttemptResult(status="failed", form_url=url)
    try:
        with sync_playwright() as playwright:
            launch: dict[str, Any] = {"headless": headless}
            if config.BROWSER_PATH:
                launch["executable_path"] = config.BROWSER_PATH
            browser = playwright.chromium.launch(**launch)
            context = browser.new_context(viewport={"width": 1280, "height": 1600})
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            try:
                _open_apply_form(page, url)
                result.form_url = page.url
                _fill_form(page, values, result)

                if screenshot_path:
                    Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(screenshot_path), full_page=True)
                    result.screenshot = str(screenshot_path)

                if not auto_submit:
                    result.status = "needs_review"
                    result.detail = (
                        f"Filled {len(result.filled)} field(s). "
                        f"{len(result.unfilled)} left for you. Not submitted — open the "
                        "posting, check the form, and send it yourself."
                    )
                elif result.blocking:
                    result.status = "needs_review"
                    labels = ", ".join(item["label"] for item in result.blocking[:5])
                    result.detail = (
                        f"Auto-submit held back: {len(result.blocking)} required field(s) "
                        f"still unanswered ({labels})."
                    )
                else:
                    result.status, result.detail = _submit(page, result)
            finally:
                context.close()
                browser.close()
    except Exception as exc:  # noqa: BLE001 - reported, never raised into the agent
        result.status = "failed"
        result.detail = f"{type(exc).__name__}: {exc}"[:500]
    return result


def _submit(page, result: AttemptResult) -> tuple[str, str]:
    for element in page.query_selector_all("button, input[type=submit], a[role=button]"):
        try:
            text = (element.inner_text() or element.get_attribute("value") or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if _NEXT_RE.match(text):
            continue
        if text and _SUBMIT_RE.search(text) and element.is_visible():
            element.click()
            page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT_MS)
            return "submitted", f"Submitted via '{text}' with {len(result.filled)} field(s) filled."
    return "needs_review", "Could not find a submit button — finish this one by hand."
