from html.parser import HTMLParser
import re
from urllib.parse import urlsplit


_WEB_URL = (
    r"(?<![\w@./-])(?:https?://|www\.|(?:[a-z0-9-]+\.)+[a-z]{2,}/)"
    r'''(?:[^\s<>()[\]{}"'“”‘’。！？、，；：]|\([^\s<>()]+\))*'''
)
_NARRATION_LINK = re.compile(
    rf"(?P<space>[^\S\n]?)(?:\(\s*{_WEB_URL}\s*\)|\[\s*{_WEB_URL}\s*\]|(?P<url>{_WEB_URL}))",
    re.IGNORECASE,
)
_DESTINATION_PREFIX = re.compile(r"\b(?:at|from|go to|open|see|visit)\s*$", re.IGNORECASE)
_DECORATIVE_SEPARATOR = re.compile(r"[-–—_*=~•·●◆◇✦✧─━#.\s]+")


def clean_narration_text(text):
    """Omit bare links except for hostnames used as destinations in prose."""
    def replace_link(match):
        prefix = text[:match.start()]
        url = match["url"]
        punctuation = ""
        if url:
            address = url.rstrip(".,;:!?…")
            punctuation = url[len(address):] if prefix.strip() else ""
            if _DESTINATION_PREFIX.search(prefix):
                try:
                    host = urlsplit(address if address.lower().startswith(("http://", "https://"))
                                    else "//" + address).hostname
                except ValueError:
                    host = None
                if host:
                    return match["space"] + host.removeprefix("www.") + punctuation
        following = text[match.end():match.end() + 1]
        if punctuation or not following or following.isspace() or following in ".,;:!?…":
            return punctuation
        return match["space"]

    cleaned = _NARRATION_LINK.sub(replace_link, text).strip()
    if _DECORATIVE_SEPARATOR.fullmatch(cleaned):
        return ""
    if cleaned != text.strip() and not any(char.isalnum() for char in cleaned):
        return ""
    return cleaned


class ArticleParser(HTMLParser):
    boundaries = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "br", "hr", "tr", "pre"}
    kinds = {**{f"h{level}": "heading" for level in range(1, 7)},
             "li": "list_item", "blockquote": "blockquote", "pre": "pre", "code": "code"}
    excluded = {"script", "style", "noscript", "svg", "nav"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.blocks = []
        self.context = []
        self.hidden = []

    def flush(self):
        text = re.sub(r"\s+", " ", "".join(self.parts)).strip()
        self.parts.clear()
        if not text:
            return
        tag = self.context[-1] if self.context else "p"
        block = {"type": self.kinds.get(tag, "paragraph"), "text": text}
        if block["type"] == "heading":
            block["level"] = int(tag[1])
        self.blocks.append(block)

    def handle_starttag(self, tag, attrs):
        if tag in self.excluded:
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag == "code" and "pre" not in self.context:
            return
        if tag in self.boundaries or tag == "code":
            self.flush()
        if tag in self.kinds:
            self.context.append(tag)

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        else:
            if tag in self.boundaries or (tag == "code" and "code" in self.context):
                self.flush()
            if tag in self.context:
                index = len(self.context) - 1 - self.context[::-1].index(tag)
                del self.context[index:]

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def article_blocks(html):
    """Preserve narration-relevant HTML structure without treating source wraps as breaks."""
    parser = ArticleParser()
    parser.feed(html)
    parser.close()
    parser.flush()
    return parser.blocks


def article_text(html):
    return "\n\n".join(block["text"] for block in article_blocks(html))


def narration_input(blocks, pause_template):
    """Render block boundaries as explicit pauses, with no leading/trailing silence."""
    before = {"heading": 0.8, "blockquote": 0.6}
    after = {"title": 1.0, "heading": 0.6, "blockquote": 0.6, "list_item": 0.3}
    blocks = [block for block in blocks if block["text"].strip()]
    pauses = [max(after.get(previous["type"], 0.4), before.get(current["type"], 0))
              for previous, current in zip(blocks, blocks[1:])]
    # Stay below Kokoro-FastAPI's default 300s total-pause limit on very long articles.
    scale = min(1.0, 240 / sum(pauses)) if pauses else 1.0
    parts = []
    for index, block in enumerate(blocks):
        if index:
            duration = f"{pauses[index - 1] * scale:.3f}".rstrip("0").rstrip(".")
            parts.append(pause_template.format(duration=duration))
        parts.append(block["text"])
    return "\n\n".join(parts)


def kokoro_input(blocks):
    return narration_input(blocks, "[pause:{duration}s]")
