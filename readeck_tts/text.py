from html.parser import HTMLParser
import re


class ArticleParser(HTMLParser):
    blocks = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "br", "tr", "pre"}
    excluded = {"script", "style", "noscript", "svg", "nav"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in self.excluded:
            self.hidden.append(tag)
        if not self.hidden and tag in self.blocks:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in self.blocks:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def article_text(html):
    parser = ArticleParser()
    parser.feed(html)
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return "\n\n".join(line for line in lines if line)
