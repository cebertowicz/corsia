from types import SimpleNamespace

import pytest

from connections.google.client import GoogleSignInRejected
from connections.mailhost.errors import MailHostError, PASSWORD_REFUSED, SERVER_UNREACHABLE
from tools.google_email import _font


async def _raises(error):
    async def sent_html(_account_id):
        raise error
    return await _font(SimpleNamespace(sent_html=sent_html), 'account')


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [GoogleSignInRejected(), MailHostError(PASSWORD_REFUSED)])
async def test_sign_in_rejection_is_not_a_font_fallback(error):
    with pytest.raises(type(error)):
        await _raises(error)


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [ValueError('decode failed'), MailHostError(SERVER_UNREACHABLE)])
async def test_read_and_decode_failures_have_a_visible_fallback(error):
    face, size, note = await _raises(error)
    assert face is None and size is None
    assert note == "Couldn’t read your usual font, so this email may go out in your mail’s default font."


@pytest.mark.asyncio
async def test_programming_error_is_not_a_font_fallback():
    with pytest.raises(TypeError):
        await _raises(TypeError('programming error'))
