import re
import unittest

from readeck_tts.text import article_blocks, article_text, kokoro_input


class NarrationTests(unittest.TestCase):
    def test_headings_keep_levels_and_inline_text(self):
        blocks = article_blocks('<h1>A <em>title</em></h1><h2>Section</h2><h6>Detail</h6>'
                                '<p>Some &amp; more\n text with <a href="/">a link</a>.</p>')
        self.assertEqual(blocks, [
            {"type": "heading", "text": "A title", "level": 1},
            {"type": "heading", "text": "Section", "level": 2},
            {"type": "heading", "text": "Detail", "level": 6},
            {"type": "paragraph", "text": "Some & more text with a link."},
        ])

    def test_source_line_wraps_do_not_create_paragraphs(self):
        self.assertEqual(article_text('<p>A sentence\nwrapped across\n source lines.</p>'),
                         "A sentence wrapped across source lines.")

    def test_nested_wrappers_and_empty_blocks_do_not_duplicate_boundaries(self):
        blocks = article_blocks('<article><div><p>First.</p><p> </p></div>'
                                '<section><h2>Next</h2><p>Second.</p></section></article>')
        self.assertEqual([block["text"] for block in blocks], ["First.", "Next", "Second."])
        self.assertEqual(kokoro_input(blocks), "First.\n\n[pause:0.8s]\n\nNext"
                         "\n\n[pause:0.6s]\n\nSecond.")

    def test_lists_and_quotes_keep_their_type_through_nested_paragraphs(self):
        blocks = article_blocks('<ul><li><p>One.</p><p>More.</p></li><li>Two.</li></ul>'
                                '<blockquote><p>Quoted.</p></blockquote><p>Normal.</p>')
        self.assertEqual([block["type"] for block in blocks],
                         ["list_item", "list_item", "list_item", "blockquote", "paragraph"])
        self.assertEqual([block["text"] for block in blocks],
                         ["One.", "More.", "Two.", "Quoted.", "Normal."])

    def test_hidden_content_and_its_boundaries_are_ignored(self):
        blocks = article_blocks('<nav><div>Skip</div><nav>Also skip</nav></nav>'
                                '<p>Hello <script>bad()</script><style>bad</style>world.</p>'
                                '<svg><text>Skip</text></svg><p>End.</p>')
        self.assertEqual([block["text"] for block in blocks], ["Hello world.", "End."])

    def test_plain_text_breaks_and_unclosed_final_paragraph(self):
        self.assertEqual(article_text('Plain text<br/>Next line<p>Last paragraph'),
                         "Plain text\n\nNext line\n\nLast paragraph")

    def test_pause_rules_without_leading_trailing_or_stacked_pauses(self):
        blocks = [{"type": "title", "text": "Title"},
                  {"type": "heading", "level": 2, "text": "Heading"},
                  {"type": "paragraph", "text": "First."},
                  {"type": "paragraph", "text": "Second."},
                  {"type": "list_item", "text": "One."},
                  {"type": "list_item", "text": "Two."},
                  {"type": "blockquote", "text": "A quotation."},
                  {"type": "paragraph", "text": "End."}]
        self.assertEqual(kokoro_input(blocks),
                         "Title\n\n[pause:1s]\n\nHeading\n\n[pause:0.6s]\n\nFirst."
                         "\n\n[pause:0.4s]\n\nSecond.\n\n[pause:0.4s]\n\nOne."
                         "\n\n[pause:0.3s]\n\nTwo.\n\n[pause:0.6s]\n\nA quotation."
                         "\n\n[pause:0.6s]\n\nEnd.")

    def test_empty_and_single_block_articles_have_no_pause(self):
        self.assertEqual(kokoro_input(article_blocks('<p> </p><script>bad</script>')), "")
        self.assertEqual(kokoro_input([{"type": "paragraph", "text": "Only."}]), "Only.")

    def test_very_long_articles_stay_under_default_server_pause_budget(self):
        blocks = [{"type": "heading", "level": 2, "text": "Section"}] * 1000
        pauses = re.findall(r"\[pause:([\d.]+)s\]", kokoro_input(blocks))
        self.assertEqual(len(pauses), 999)
        self.assertTrue(all(float(pause) > 0 for pause in pauses))
        self.assertLess(sum(float(pause) for pause in pauses), 300)


if __name__ == "__main__":
    unittest.main()
