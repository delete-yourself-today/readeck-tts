from html.parser import HTMLParser
import re


class ArticleParser(HTMLParser):
    boundaries = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "br", "hr", "tr", "pre"}
    kinds = {**{f"h{level}": "heading" for level in range(1, 7)},
             "li": "list_item", "blockquote": "blockquote", "pre": "pre"}
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
        if tag in self.boundaries:
            self.flush()
        if tag in self.kinds:
            self.context.append(tag)

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        else:
            if tag in self.boundaries:
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


def kokoro_input(blocks):
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
            parts.append(f"[pause:{duration}s]")
        parts.append(block["text"])
    return "\n\n".join(parts)
