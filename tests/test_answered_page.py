from dispatcher import answered_page


def test_an_already_disabled_control_is_not_disabled_twice():
    assert answered_page.render('<button disabled>Send</button>', {}) == '<button disabled>Send</button>'


def test_answers_that_are_not_an_object_are_shown_as_text():
    assert answered_page.render("<body></body>", "csv <please>") == (
        "<body><dl><dt>answers</dt><dd>csv &lt;please&gt;</dd></dl></body>")


def test_a_filled_note_replaces_the_textarea_markup_too():
    page = '<textarea id="note-q"><b>old</b></textarea>'
    assert answered_page.render(page, {"q.note": "new"}) == (
        '<textarea id="note-q" disabled>new</textarea>')
