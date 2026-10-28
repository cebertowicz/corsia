"""Font detection and multipart composition for reviewed mail."""
from __future__ import annotations

import html
import re
from collections import Counter
from email.message import EmailMessage
from html.parser import HTMLParser


class _Fonts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.faces: Counter[str] = Counter()
        self.sizes: Counter[str] = Counter()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        face = values.get("face") if tag.lower() == "font" else None
        style = values.get("style") or ""
        family = re.search(r"(?:^|;)\s*font-family\s*:\s*([^;]+)", style, re.I)
        size = re.search(r"(?:^|;)\s*font-size\s*:\s*([^;]+)", style, re.I)
        face = face or (family.group(1).strip() if family else None)
        if face:
            self.faces[face.strip().strip("\"'")] += 1
        raw_size = size.group(1).strip() if size else (values.get("size") if tag.lower() == "font" else None)
        if raw_size:
            self.sizes[{"1": "x-small", "2": "small", "3": "medium", "4": "large", "5": "x-large"}.get(raw_size, raw_size)] += 1


def detected_font(samples: list[str]) -> tuple[str | None, str | None]:
    faces: Counter[str] = Counter()
    sizes: Counter[str] = Counter()
    for sample in samples:
        parser = _Fonts()
        parser.feed(sample)
        if parser.faces:
            faces[parser.faces.most_common(1)[0][0]] += 1
        if parser.sizes:
            sizes[parser.sizes.most_common(1)[0][0]] += 1
    return (faces.most_common(1)[0][0] if faces else None,
            sizes.most_common(1)[0][0] if sizes else None)


def add_html(message: EmailMessage, body: str, face: str | None, size: str | None,
             quote_text: str | None = None) -> None:
    """Add a plain alternative and HTML, quoting the original in Gmail's foldable block."""
    safe_body = html.escape(body).replace("\n", "<br>\n")
    attributes = ""
    if face:
        attributes += f' face="{html.escape(face, quote=True)}"'
    if size:
        attributes += f' style="font-size:{html.escape(size, quote=True)}"'
    content = f'<div dir="ltr"><font{attributes}>{safe_body}</font></div>'
    if quote_text:
        quoted = html.escape(quote_text).replace("\n", "<br>\n")
        content += f'<div class="gmail_quote"><blockquote class="gmail_quote">{quoted}</blockquote></div>'
    message.set_content(body + ("\n\n" + "\n".join("> " + line for line in quote_text.splitlines()) if quote_text else ""))
    message.add_alternative(content, subtype="html")
