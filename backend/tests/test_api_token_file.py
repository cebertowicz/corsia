"""The CLI API token's folder: the hardened writer and the one-time move from earlier locations."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from infra import api_token_file
from infra.api_token_file import api_token_file as token_path
from infra.api_token_file import move_legacy_token, write_secret_file


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def places(tmp_path):
    home, checkout = tmp_path / "home", tmp_path / "checkout"
    home.mkdir()
    checkout.mkdir()
    return home, checkout


def _legacy(path: Path, value: str = "earlier") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    path.chmod(0o644)
    return path


def _leftovers(home: Path) -> list[str]:
    return sorted(path.name for path in token_path(home).parent.iterdir())


def test_the_token_lives_in_its_own_folder_under_home(tmp_path) -> None:
    assert token_path(tmp_path) == tmp_path / ".corsia" / "credentials" / "api-token"


def test_the_writer_creates_a_private_folder_and_file(tmp_path) -> None:
    target = token_path(tmp_path)

    write_secret_file(target, "value")

    assert target.read_text() == "value"
    assert _mode(target) == 0o600 and _mode(target.parent) == 0o700
    assert _leftovers(tmp_path) == ["api-token"]


def test_the_writer_refuses_to_follow_a_linked_temp_path(tmp_path) -> None:
    target = token_path(tmp_path)
    target.parent.mkdir(parents=True, mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("unchanged")
    temporary = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    temporary.symlink_to(elsewhere)

    with pytest.raises(FileExistsError):
        write_secret_file(target, "value")

    assert elsewhere.read_text() == "unchanged"
    assert temporary.is_symlink() and not target.exists()


def test_a_token_at_the_earlier_home_location_moves_into_the_folder(places) -> None:
    home, checkout = places
    legacy = _legacy(home / ".corsia" / ".api-token")

    move_legacy_token(home, None, checkout)

    assert not os.path.lexists(legacy)
    assert token_path(home).read_text() == "earlier"
    assert _mode(token_path(home)) == 0o600 and _mode(token_path(home).parent) == 0o700
    assert _leftovers(home) == ["api-token"]


def test_a_token_at_the_earlier_checkout_location_moves_into_the_folder(places) -> None:
    home, checkout = places
    legacy = _legacy(checkout / ".api-token")

    move_legacy_token(home, None, checkout)

    assert not os.path.lexists(legacy)
    assert token_path(home).read_text() == "earlier"
    assert _mode(token_path(home)) == 0o600


def test_an_earlier_copy_is_deleted_when_the_folder_already_holds_a_token(places) -> None:
    home, checkout = places
    write_secret_file(token_path(home), "current")
    legacies = (_legacy(home / ".corsia" / ".api-token"), _legacy(checkout / ".api-token"))

    move_legacy_token(home, None, checkout)

    assert not any(os.path.lexists(path) for path in legacies)
    assert token_path(home).read_text() == "current"
    assert _leftovers(home) == ["api-token"]


def test_a_move_across_filesystems_writes_the_new_file_then_removes_the_old(places, monkeypatch) -> None:
    home, checkout = places
    legacy = _legacy(home / ".corsia" / ".api-token")

    def cross_device(*_args):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(api_token_file.os, "rename", cross_device)
    move_legacy_token(home, None, checkout)

    assert not os.path.lexists(legacy)
    assert token_path(home).read_text() == "earlier"
    assert _mode(token_path(home)) == 0o600
    assert _leftovers(home) == ["api-token"]


def test_an_interrupted_move_finishes_on_the_next_run(places, monkeypatch) -> None:
    home, checkout = places
    legacy = _legacy(home / ".corsia" / ".api-token")

    def cross_device(*_args):
        raise OSError(errno.EXDEV, "cross-device link")

    def stop_before_removal(self, *args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(api_token_file.os, "rename", cross_device)
        patch.setattr(Path, "unlink", stop_before_removal)
        with pytest.raises(KeyboardInterrupt):
            move_legacy_token(home, None, checkout)
    assert legacy.exists() and token_path(home).read_text() == "earlier"

    move_legacy_token(home, None, checkout)

    assert not os.path.lexists(legacy)
    assert token_path(home).read_text() == "earlier"
    assert _mode(token_path(home)) == 0o600
    assert _leftovers(home) == ["api-token"]


def test_a_linked_earlier_copy_is_written_into_the_folder_and_the_link_removed(places) -> None:
    home, checkout = places
    chosen = _legacy(home / "chosen-token")
    link = home / ".corsia" / ".api-token"
    link.parent.mkdir(parents=True)
    link.symlink_to(chosen)

    move_legacy_token(home, None, checkout)

    assert not os.path.lexists(link)
    assert not token_path(home).is_symlink() and token_path(home).read_text() == "earlier"
    assert _mode(token_path(home)) == 0o600


def test_a_file_named_by_the_override_stays_where_it_is(places) -> None:
    home, checkout = places
    chosen = _legacy(home / ".corsia" / ".api-token")

    move_legacy_token(home, chosen, checkout)

    assert chosen.read_text() == "earlier"
    assert not token_path(home).exists()


def test_nothing_happens_without_an_earlier_copy(places) -> None:
    home, checkout = places

    move_legacy_token(home, None, checkout)

    assert not (home / ".corsia").exists()
