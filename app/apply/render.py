"""Markdown resume -> PDF, with no dependencies.

An ATS upload field wants a PDF. Pulling in a rendering stack for what is
fundamentally "lay out some Helvetica" isn't worth it, so this writes the PDF
byte structure directly: one content stream per page, the base-14 fonts (which
every reader has built in, so nothing needs embedding), and a correct xref
table. The output is text-selectable, which is what ATS parsers actually read.

Deliberately plain: one column, no colour, no tables. That is also the format
resume parsers get right most often.
"""

from __future__ import annotations

import re
from pathlib import Path

PAGE_WIDTH, PAGE_HEIGHT = 612.0, 792.0  # US Letter, in points
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 54.0, 54.0, 54.0

BODY_SIZE, BODY_LEADING = 9.8, 13.0
H1_SIZE, H2_SIZE, H3_SIZE = 17.0, 11.2, 10.2

FONTS = {"regular": "F1", "bold": "F2", "italic": "F3"}

# Average glyph widths as a fraction of font size. Helvetica's real metrics vary
# per character; this approximation is close enough to wrap at a sane column and
# keeps the module free of a font-metrics table.
_CHAR_WIDTH = {"regular": 0.50, "bold": 0.53, "italic": 0.50}


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _wrap(text: str, style: str, size: float, width: float) -> list[str]:
    limit = max(8, int(width / (size * _CHAR_WIDTH[style])))
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= limit or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


class _Line:
    __slots__ = ("text", "style", "size", "indent", "space_before")

    def __init__(self, text: str, style: str, size: float, indent: float = 0.0,
                 space_before: float = 0.0):
        self.text = text
        self.style = style
        self.size = size
        self.indent = indent
        self.space_before = space_before


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_CODE_RE = re.compile(r"`([^`]+)`")


def _plain(text: str) -> str:
    """Flatten inline Markdown. Emphasis can't survive in a single-run text
    object, so it is dropped rather than rendered as literal asterisks."""
    text = _LINK_RE.sub(r"\1 (\2)", text)
    text = _BOLD_RE.sub(r"\1", text)
    text = _ITALIC_RE.sub(r"\1", text)
    text = _CODE_RE.sub(r"\1", text)
    return text.replace("—", "-").replace("–", "-").replace(" ", " ")


def _layout(markdown: str) -> list[_Line]:
    """Markdown -> a flat list of positioned text lines."""
    usable = PAGE_WIDTH - 2 * MARGIN_X
    lines: list[_Line] = []
    first_block = True

    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        numbered = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)

        if heading:
            level = len(heading.group(1))
            size = H1_SIZE if level == 1 else H2_SIZE if level == 2 else H3_SIZE
            gap = 0.0 if first_block else (10.0 if level <= 2 else 7.0)
            for index, part in enumerate(_wrap(_plain(heading.group(2)), "bold", size, usable)):
                lines.append(_Line(part, "bold", size, 0.0, gap if index == 0 else 0.0))
        elif bullet:
            body = _plain(bullet.group(1))
            wrapped = _wrap(body, "regular", BODY_SIZE, usable - 12.0)
            lines.append(_Line(f"•  {wrapped[0]}", "regular", BODY_SIZE, 6.0, 1.5))
            for part in wrapped[1:]:
                lines.append(_Line(part, "regular", BODY_SIZE, 18.0))
        elif numbered:
            body = _plain(numbered.group(2))
            wrapped = _wrap(body, "regular", BODY_SIZE, usable - 14.0)
            lines.append(_Line(f"{numbered.group(1)}. {wrapped[0]}", "regular", BODY_SIZE, 6.0, 1.5))
            for part in wrapped[1:]:
                lines.append(_Line(part, "regular", BODY_SIZE, 20.0))
        else:
            for index, part in enumerate(_wrap(_plain(stripped), "regular", BODY_SIZE, usable)):
                lines.append(_Line(part, "regular", BODY_SIZE, 0.0, 4.0 if index == 0 else 0.0))

        first_block = False

    return lines


def _paginate(lines: list[_Line]) -> list[list[tuple[float, _Line]]]:
    """Assign each line a baseline, breaking pages at the bottom margin."""
    pages: list[list[tuple[float, _Line]]] = []
    page: list[tuple[float, _Line]] = []
    cursor = PAGE_HEIGHT - MARGIN_TOP

    for line in lines:
        leading = BODY_LEADING if line.size <= BODY_SIZE else line.size * 1.3
        cursor -= line.space_before + leading
        if cursor < MARGIN_BOTTOM:
            pages.append(page)
            page = []
            cursor = PAGE_HEIGHT - MARGIN_TOP - leading
        page.append((cursor, line))

    pages.append(page)
    return [p for p in pages if p] or [[]]


def _content_stream(page: list[tuple[float, _Line]]) -> bytes:
    parts = ["BT"]
    for baseline, line in page:
        parts.append(f"/{FONTS[line.style]} {line.size:.1f} Tf")
        parts.append(f"1 0 0 1 {MARGIN_X + line.indent:.1f} {baseline:.1f} Tm")
        parts.append(f"({_escape(line.text)}) Tj")
    parts.append("ET")
    # cp1252, not latin-1: the fonts declare /WinAnsiEncoding, which is where the
    # bullet, the curly quotes, and the dashes actually live.
    return "\n".join(parts).encode("cp1252", "replace")


def markdown_to_pdf(markdown: str, path: str | Path) -> Path:
    """Render `markdown` to a PDF at `path` and return the path."""
    pages = _paginate(_layout(markdown or ""))

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)  # object numbers are 1-based

    font_ids = {
        "regular": add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                       b"/Encoding /WinAnsiEncoding >>"),
        "bold": add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                    b"/Encoding /WinAnsiEncoding >>"),
        "italic": add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique "
                      b"/Encoding /WinAnsiEncoding >>"),
    }
    resources = (
        "<< /Font << " +
        " ".join(f"/{FONTS[style]} {font_ids[style]} 0 R" for style in FONTS) +
        " >> >>"
    ).encode("ascii")

    # The pages object has to know its kids before they exist, so reserve its number.
    pages_obj = add(b"")
    page_objs: list[int] = []
    for page in pages:
        stream = _content_stream(page)
        content_obj = add(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
            + stream + b"\nendstream"
        )
        page_objs.append(add(
            b"<< /Type /Page /Parent " + str(pages_obj).encode("ascii") + b" 0 R "
            b"/MediaBox [0 0 " + f"{PAGE_WIDTH:.0f} {PAGE_HEIGHT:.0f}".encode("ascii") + b"] "
            b"/Resources " + resources + b" /Contents "
            + str(content_obj).encode("ascii") + b" 0 R >>"
        ))

    kids = " ".join(f"{num} 0 R" for num in page_objs)
    objects[pages_obj - 1] = (
        f"<< /Type /Pages /Count {len(page_objs)} /Kids [{kids}] >>".encode("ascii")
    )
    catalog = add(f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode("ascii"))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(bytes(out))
    return destination


def markdown_to_text(markdown: str, path: str | Path) -> Path:
    """Plain-text fallback, for the rare form that rejects PDFs."""
    body = "\n".join(f"{' ' * int(line.indent / 6)}{line.text}" for line in _layout(markdown or ""))
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(body + "\n", encoding="utf-8")
    return destination
