from connections.mail.mail_compose import detected_font


def test_font_vote_is_per_sent_message_and_recognizes_html_size():
    samples = [
        '<font face="Arial" size="3">one</font>' * 10,
        '<font face="Garamond" size="2">two</font>',
        '<font face="Garamond" style="font-size:small">three</font>',
    ]
    assert detected_font(samples) == ("Garamond", "small")


def test_absent_font_uses_provider_default():
    assert detected_font(["<div>Plain message</div>"]) == (None, None)
