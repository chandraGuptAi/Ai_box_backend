"""Server-side Markdown, Word-compatible HTML and safe RFC4180 CSV exports."""

from __future__ import annotations

import csv
import html
import io
import re
from ..errors import EscalationError


def tables(text: str) -> list[list[list[str]]]:
    result = []
    current = []
    for line in text.splitlines() + [""]:
        if line.strip().startswith("|") and "|" in line.strip()[1:]:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cells):
                current.append(cells)
        elif current:
            result.append(current)
            current = []
    return result


def inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)


def render(text: str) -> str:
    result = []
    in_code = False
    for line in text.splitlines():
        if line.startswith("```"):
            result.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
        elif in_code:
            result.append(html.escape(line) + "\n")
        elif line.startswith("#"):
            level = min(4, max(2, len(line) - len(line.lstrip("#"))))
            result.append(f"<h{level}>" + inline(line.lstrip("# ")) + f"</h{level}>")
        elif line.startswith(("- ", "* ")):
            result.append("<p>• " + inline(line[2:]) + "</p>")
        else:
            result.append("<p>" + inline(line) + "</p>")
    if in_code:
        result.append("</pre>")
    return '<html><meta charset="utf-8"><body>' + "".join(result) + "</body></html>"


def export(text: str, format: str) -> tuple[bytes, str]:
    if format == "md":
        return text.encode("utf-8"), "text/markdown; charset=utf-8"
    if format == "doc":
        return render(text).encode("utf-8"), "application/msword"
    if format == "csv":
        found = tables(text)
        if not found:
            raise EscalationError(404, "Report has no table")
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        for row in found[0]:
            writer.writerow(["'" + value if re.match(r"^\s*[=+@-]", value) else value for value in row])
        return output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"
    raise EscalationError(400, "Unsupported export format")
