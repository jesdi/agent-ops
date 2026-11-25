from dispatcher import answered_page


def test_an_already_disabled_control_is_not_disabled_twice():
    assert answered_page.render('<button disabled>Send</button>', {}) == '<button disabled>Send</button>'
