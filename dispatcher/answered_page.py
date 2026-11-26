"""The answered page: a request page with a set of answers filled in and
every control disabled, rendered in markup so it reads right with scripts off.

The page is re-emitted as parsed; only the form controls are rewritten. Answer
values and notes go in as text, never as markup."""
from __future__ import annotations

import json
from html import escape
from html.parser import HTMLParser

_CONTROLS = frozenset({"input", "textarea", "select", "button"})


def render(page: str, answers: object) -> str:
    """`page` with the radio named `q` of value `answers[q]` checked (every
    other radio unchecked), the note `answers["q.note"]` in textarea
    `note-q`, and every input, textarea, select and button disabled. Every
    answer that lands on none of them is listed as text before `</body>`."""
    renderer = _Renderer(answers if isinstance(answers, dict) else {"answers": answers})
    renderer.feed(page)
    renderer.close()
    unmatched = "".join(
        f"<dt>{escape(str(key), quote=False)}</dt><dd>{escape(_text(value), quote=False)}</dd>"
        for key, value in renderer.answers.items() if key not in renderer.matched)
    if unmatched:
        at = renderer.body_end if renderer.body_end is not None else len(renderer.out)
        renderer.out.insert(at, f"<dl>{unmatched}</dl>")
    return "".join(renderer.out)


def _text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _tag(tag: str, attrs: list[tuple[str, str | None]], close: str) -> str:
    parts = [tag] + [name if value is None else f'{name}="{escape(value)}"' for name, value in attrs]
    return f"<{' '.join(parts)}{close}>"


class _Renderer(HTMLParser):
    def __init__(self, answers: dict):
        super().__init__(convert_charrefs=False)
        self.answers, self.out, self.skipping = answers, [], False
        self.matched: set = set()
        self.body_end: int | None = None

    def _control(self, tag: str, attrs: list, close: str) -> str:
        values = dict(attrs)
        text = self._note(values) if tag == "textarea" else ""
        if tag == "input" and (values.get("type") or "").lower() == "radio":
            attrs = self._radio(attrs, values)
        if "disabled" not in values:
            attrs = [*attrs, ("disabled", None)]
        return _tag(tag, attrs, close) + text

    def _radio(self, attrs: list, values: dict) -> list:
        attrs = [(k, v) for k, v in attrs if k != "checked"]
        value = values.get("value")
        if value is not None and self.answers.get(values.get("name")) == value:
            attrs.append(("checked", None))
            self.matched.add(values.get("name"))
        return attrs

    def _note(self, values: dict) -> str:
        textarea_id = values.get("id") or ""
        if not textarea_id.startswith("note-"):
            return ""
        key = textarea_id.removeprefix("note-") + ".note"
        note = self.answers.get(key)
        if not isinstance(note, str):
            return ""
        self.matched.add(key)
        self.skipping = True
        return escape(note, quote=False)

    def _start(self, tag, attrs, close):
        if self.skipping:   # markup inside a filled textarea (Python < 3.12 parses it)
            return
        if tag in _CONTROLS:
            self.out.append(self._control(tag, attrs, close))
        else:
            self.out.append(self.get_starttag_text())

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, "")

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, " /")

    def handle_endtag(self, tag):
        if tag == "textarea":
            self.skipping = False
        elif self.skipping:
            return
        if tag == "body":
            self.body_end = len(self.out)
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skipping:
            self.out.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")

    def handle_comment(self, data):
        if not self.skipping:
            self.out.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.out.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.out.append(f"<?{data}>")

    def unknown_decl(self, data):
        self.out.append(f"<![{data}]>")


if __name__ == "__main__":
    page = ('<!doctype html><p>a &amp; b</p><input type="radio" name="t" value="s" checked>'
            '<input type="radio" name="t" value="c"/><textarea id="note-t">x</textarea><script>1<2</script>')
    out = render(page, {"t": "c", "t.note": "</textarea><b>"})
    assert out == ('<!doctype html><p>a &amp; b</p><input type="radio" name="t" value="s" disabled>'
                   '<input type="radio" name="t" value="c" checked disabled />'
                   '<textarea id="note-t" disabled>&lt;/textarea&gt;&lt;b&gt;</textarea>'
                   '<script>1<2</script>'), out
    assert 'value="s" disabled>' in render(page, {})
    out = render("<body><p>q</p></body>", {"t": "<b>", "n": [1]})
    assert out == "<body><p>q</p><dl><dt>t</dt><dd>&lt;b&gt;</dd><dt>n</dt><dd>[1]</dd></dl></body>", out
