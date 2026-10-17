from unittest.mock import ANY, Mock

import app_startup_runtime


def _reconcile(monkeypatch, *, has_account: bool, current_asset: bool, test_mode: bool = False) -> Mock:
    from core import corrections_asset_download, corrections_asset_files
    from infra import accounts

    flight = Mock()
    monkeypatch.setattr(accounts, "has_users", lambda: has_account)
    monkeypatch.setattr(corrections_asset_files, "current_asset_verified", lambda: current_asset)
    monkeypatch.setattr(corrections_asset_download, "FLIGHT", flight)
    monkeypatch.delenv(corrections_asset_download.BASE_URL_ENV, raising=False)
    if test_mode:
        monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    else:
        monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)

    app_startup_runtime._start_current_corrections_classifier(Mock())
    return flight


def test_startup_starts_download_when_the_current_asset_is_missing(monkeypatch):
    flight = _reconcile(monkeypatch, has_account=True, current_asset=False)

    flight.start.assert_called_once_with(on_failure=ANY)


def test_startup_does_nothing_when_the_current_asset_is_verified(monkeypatch):
    flight = _reconcile(monkeypatch, has_account=True, current_asset=True)

    flight.start.assert_not_called()


def test_startup_never_starts_before_an_account_exists(monkeypatch):
    flight = _reconcile(monkeypatch, has_account=False, current_asset=False)

    flight.start.assert_not_called()


def test_startup_resumes_a_download_setup_requested_before_any_account_exists(monkeypatch):
    flight = _reconcile(monkeypatch, has_account=False, current_asset=False)

    flight.resume.assert_called_once_with(on_failure=ANY)
    flight.start.assert_not_called()


def test_a_quit_download_restarts_from_its_marker_without_an_account(tmp_path, monkeypatch):
    from core.corrections_asset_download import ClassifierDownload

    flight = ClassifierDownload(root=lambda: tmp_path / "asset")
    started = Mock()
    monkeypatch.setattr(flight, "start", started)
    flight.resume()
    started.assert_not_called()  # nothing was requested

    flight._request()  # what setup's request leaves behind when the app quits mid-download
    flight.resume()
    started.assert_called_once_with(on_failure=None)


def test_a_test_run_fetches_nothing_unless_it_names_its_own_release_host(monkeypatch):
    from core import corrections_asset_download

    assert _reconcile(monkeypatch, has_account=True, current_asset=False, test_mode=True).start.called is False

    flight = Mock()
    monkeypatch.setattr(corrections_asset_download, "FLIGHT", flight)
    monkeypatch.setenv(corrections_asset_download.BASE_URL_ENV, "http://127.0.0.1:1")
    app_startup_runtime._start_current_corrections_classifier(Mock())
    flight.start.assert_called_once_with(on_failure=ANY)


class _InlineLoop:
    """Runs what a flight thread hands to the app loop at once, so a test can read the outcome."""

    @staticmethod
    def call_soon_threadsafe(callback):
        callback()


def _failed_startup_flight(tmp_path, monkeypatch, *, installed: bool) -> list[tuple]:
    from automation import update_failure_notice
    from core import corrections_asset_download as download
    from jobs import corrections_assets

    notices: list[tuple] = []
    monkeypatch.setattr(
        update_failure_notice, "tell_corrections_classifier_failure_detached",
        lambda version, reason, reader, *, tasks: notices.append((version, reason, reader)),
    )
    monkeypatch.setattr(corrections_assets, "installed", lambda: installed)
    monkeypatch.setattr(download, "PINNED_MANIFESTS", {"v1": "0" * 64})
    flight = download.ClassifierDownload(
        root=lambda: tmp_path / "asset", url=lambda: "http://127.0.0.1:1", sleep=lambda _seconds: None,
    )
    flight.start(wait=True, on_failure=app_startup_runtime._classifier_failure_reporter(_InlineLoop()))
    assert flight.status()["state"] == "failed"
    return notices


def test_a_startup_download_that_fails_with_nothing_installed_tells_the_owner_once(tmp_path, monkeypatch):
    notices = _failed_startup_flight(tmp_path, monkeypatch, installed=False)

    assert notices == [("v1", "network_lost", notices[0][2])] and notices[0][2]


def test_a_startup_download_that_fails_while_an_older_asset_serves_stays_a_warning(tmp_path, monkeypatch):
    assert _failed_startup_flight(tmp_path, monkeypatch, installed=True) == []
