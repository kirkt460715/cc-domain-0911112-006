import unittest
from pathlib import Path
from html.parser import HTMLParser

class StatementFixture(unittest.TestCase):
    def test_encoding_and_source_identity(self):
        root=Path(__file__).resolve().parents[1]/"fixtures/statements"
        first=(root/"layout-a.html").read_text()
        second=(root/"layout-b-gb18030.html").read_bytes().decode("gb18030")
        for content in [first,second]:
            HTMLParser().feed(content)
            self.assertIn("TX-001",content)
            self.assertIn("1,234.50",content)
        self.assertIn('rowspan="2"',first)
        self.assertIn("需要复核",second)
