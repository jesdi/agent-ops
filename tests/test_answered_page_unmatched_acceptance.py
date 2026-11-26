"""Acceptance tests for #282 ticket 02: answers that land on no input are shown
as text.

Spec: specs/2026-11-24-answered-page-artifact/spec.md, requirements 3 and 4.
Black-box through the same seam as ticket 01: an `answers` intent drained by
`operator_intents._apply_intents`, then the console's
`GET /api/task/{repository}/{issue}/artifacts/questionnaire-answers`.
"""
import re
from collections import Counter
from html.parser import HTMLParser

from tests.test_answered_page_capture_acceptance import PAGE, _answered, _answered_page


class _Text(HTMLParser):
    """The plain text nodes of a document (not inside script, style or a
    textarea) and the tags it opens."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.nodes, self.tags = [], Counter()
        self._hidden = 0
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags[tag] += 1
        if tag in ("script", "style", "textarea"):
            self._hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "textarea"):
            self._hidden -= 1

    def handle_data(self, data):
        if not self._hidden and data.strip():
            self.nodes.append(data.strip())


def _added_text(text):
    """The plain text the answered page adds to the request page."""
    added = Counter(_Text(text).nodes) - Counter(_Text(PAGE.decode()).nodes)
    return " ".join(added.elements())


def _shown(added, *words):
    for word in words:
        assert re.search(rf"(?<![\w.]){re.escape(word)}(?![\w.])", added), (
            f"{word!r} is not shown as text; added text: {added!r}")


def test_an_answer_with_no_input_is_shown_as_text(tmp_path, monkeypatch):
    c, _ = _answered(tmp_path, monkeypatch, {"format": "parquet", "legacy": "yes"})

    text, page = _answered_page(c, "questionnaire-answers")
    assert page.checked("format") == ["parquet"]
    added = _added_text(text)
    _shown(added, "legacy", "yes")
    assert "parquet" not in added, "a matched answer is listed again as text"


def test_a_value_no_radio_offers_and_a_note_with_no_textarea_are_shown_as_text(
        tmp_path, monkeypatch):
    c, _ = _answered(tmp_path, monkeypatch, {"format": "xml", "other.note": "see #280"})

    text, page = _answered_page(c, "questionnaire-answers")
    assert page.checked("format") == []
    added = _added_text(text)
    _shown(added, "format", "xml", "other.note", "see #280")


def test_values_that_are_not_strings_are_shown_as_text(tmp_path, monkeypatch):
    c, _ = _answered(tmp_path, monkeypatch, {"format": 3, "flags": ["a", "b"]})

    text, page = _answered_page(c, "questionnaire-answers")
    assert page.checked("format") == []
    added = _added_text(text)
    _shown(added, "format", "3", "flags", "a", "b")


def test_markup_in_an_unmatched_value_shows_literally(tmp_path, monkeypatch):
    c, _ = _answered(tmp_path, monkeypatch, {"format": "parquet", "legacy": "<b>x</b>"})

    text, _ = _answered_page(c, "questionnaire-answers")
    assert _Text(text).tags["b"] == 0, "the unmatched value added a b element"
    _shown(_added_text(text), "legacy", "<b>x</b>")
