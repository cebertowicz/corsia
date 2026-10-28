"""Client file errors distinguish missing files without exposing forbidden paths."""

import pytest

from connections.google.client import BAD_CLIENT_FILE, GoogleSignInError, read_client_file


def test_missing_client_file_has_actionable_error(tmp_path):
    with pytest.raises(GoogleSignInError) as error:
        read_client_file(str(tmp_path / 'missing.json'))
    assert str(error.value) == "There's no file at that path. Pass the path of the client JSON you downloaded from Google Cloud."


def test_existing_non_client_file_has_generic_error(tmp_path):
    path = tmp_path / 'other.json'
    path.write_text('{"web": {}}')
    with pytest.raises(GoogleSignInError) as error:
        read_client_file(str(path))
    assert str(error.value) == BAD_CLIENT_FILE


@pytest.mark.parametrize('exists', [True, False])
def test_forbidden_path_has_same_generic_error(tmp_path, exists):
    path = tmp_path / '.env.json'
    if exists:
        path.write_text('{}')
    with pytest.raises(GoogleSignInError) as error:
        read_client_file(str(path))
    assert str(error.value) == BAD_CLIENT_FILE
