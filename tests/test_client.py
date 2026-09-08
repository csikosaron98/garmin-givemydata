"""Regression tests for garmin_client.client."""

import inspect
from unittest import mock
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from selenium.common.exceptions import TimeoutException

from garmin_client.client import GarminClient, _ProcessLifecycle, _chromium_binary


class TestProcessLifecycleThreadSafety(unittest.TestCase):
    """Issue #35 bug 2: _ProcessLifecycle.install() used to call
    signal.signal() unconditionally. The MCP server runs sync in a
    ThreadPoolExecutor worker, so install() runs from a non-main thread
    and signal.signal() raises ValueError("signal only works in main
    thread of the main interpreter").
    """

    def test_install_from_worker_thread_does_not_raise(self):
        errors: list[BaseException] = []

        def worker():
            try:
                lifecycle = _ProcessLifecycle(cleanup_fn=lambda: None)
                lifecycle.install()
            except BaseException as exc:
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        self.assertEqual(errors, [], f"install() raised in worker thread: {errors}")


class TestFetchBatchResilience(unittest.TestCase):
    """A single stalled request used to kill an entire multi-year sync:
    _fetch_batch's execute_async_script would hit Selenium's 120s script
    timeout and the TimeoutException propagated uncaught out of fetch_all.
    """

    def _client(self) -> GarminClient:
        tmp = tempfile.mkdtemp(prefix="garmin-test-profile-")
        return GarminClient("test@example.com", "pw", profile_dir=Path(tmp))

    def test_retries_after_transient_timeout(self):
        client = self._client()
        attempts = []
        payload = {"steps_2025-01-01": {"status": 200, "data": {"steps": 1}}}

        def fake_once(rest, gql):
            attempts.append(1)
            if len(attempts) == 1:
                raise TimeoutException("script timeout")
            return payload

        client._fetch_batch_once = fake_once
        with patch("garmin_client.client.time.sleep"):
            result = client._fetch_batch({"steps_2025-01-01": "/url"}, {})

        self.assertEqual(result, payload)
        self.assertEqual(len(attempts), 2)

    def test_persistent_failure_returns_empty_instead_of_raising(self):
        client = self._client()
        attempts = []

        def fake_once(rest, gql):
            attempts.append(1)
            raise TimeoutException("script timeout")

        client._fetch_batch_once = fake_once
        with patch("garmin_client.client.time.sleep"):
            result = client._fetch_batch({"steps_2025-01-01": "/url"}, {})

        self.assertEqual(result, {})
        self.assertEqual(len(attempts), 3)

    def test_in_page_fetches_have_abort_timeout(self):
        """Guard: every fetch() inside the batch script must carry an
        AbortSignal timeout, otherwise one stalled request hangs the whole
        script until Selenium's script timeout kills the sync."""
        source = inspect.getsource(GarminClient._fetch_batch_once)
        self.assertIn("AbortSignal.timeout", source)


class TestMfaRaceCondition(unittest.TestCase):
    """login()'s "stuck on SSO after MFA" recovery used to fire on
    ``mfa_prompted`` (true the instant the MFA page appears) instead of on
    whether the code had actually been submitted yet. On an account where
    typing/retrieving the MFA code takes longer than ~30s, the recovery
    navigated away from the MFA page (self._driver.get(CONNECT_URL)) before
    the user's code could be submitted, disrupting the page state and making
    the subsequent _submit_mfa_code() selector search fail with "Could not
    find MFA input field to fill" even though the code itself was correct.

    The fix: track submission with its own ``mfa_submitted`` flag, set only
    after _submit_mfa_code() is called, and gate the recovery navigation on
    that flag instead of ``mfa_prompted``. These are source-level guards
    (rather than driving the full polling loop with a mocked driver) because
    login() is long, deeply nested, and Selenium-coupled; they still fail
    loudly if the gating regresses.
    """

    def test_login_source_declares_mfa_submitted_flag(self):
        source = inspect.getsource(GarminClient.login)
        self.assertIn(
            "mfa_submitted = False",
            source,
            "login() must track whether the MFA code was actually submitted, "
            "separately from whether the MFA page was merely seen.",
        )

    def test_stuck_on_sso_recovery_gated_on_submission_not_prompt(self):
        source = inspect.getsource(GarminClient.login)
        self.assertIn(
            "if mfa_submitted and poll > 0 and poll % 30 == 0:",
            source,
            "The 'stuck on SSO after MFA' recovery (which navigates away from "
            "the MFA page) must wait until the code was submitted, not just "
            "until the MFA page was detected — otherwise it can fire while "
            "the user is still typing the code and knock them off the page.",
        )
        self.assertNotIn(
            "if mfa_prompted and poll > 0 and poll % 30 == 0:",
            source,
            "Regression: the SSO-recovery navigation is gated on mfa_prompted "
            "again, which reintroduces the race condition described above.",
        )

    def test_mfa_submitted_flag_set_right_after_submit_call(self):
        source = inspect.getsource(GarminClient.login)
        self.assertIn(
            "self._submit_mfa_code(code)\n                mfa_submitted = True",
            source,
            "mfa_submitted must be set immediately after _submit_mfa_code() "
            "is called, so the SSO-recovery check above can rely on it.",
        )


if __name__ == "__main__":
    unittest.main()


class TestChromiumDetection(unittest.TestCase):
    """The VM needs Selenium pointed at its snap-installed Chromium.

    That setting lived on the server as an uncommitted edit, present in no git
    history — one `git checkout` from silently breaking Garmin login the next
    time the session needed re-authenticating. It is detected here instead, so
    the same checkout runs on the VM and on a laptop.
    """

    def test_an_explicit_override_wins(self):
        with mock.patch.dict(os.environ, {"CHROMIUM_BINARY": "/opt/my/chrome"}):
            self.assertEqual(_chromium_binary(), "/opt/my/chrome")

    def test_the_snap_path_is_used_when_it_exists(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("garmin_client.client._os.path.exists", lambda p: p == "/snap/bin/chromium"):
            self.assertEqual(_chromium_binary(), "/snap/bin/chromium")

    def test_nothing_is_forced_on_an_ordinary_machine(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("garmin_client.client._os.path.exists", lambda p: False):
            self.assertIsNone(
                _chromium_binary(),
                "forcing a binary_location on a machine with a normal Chrome "
                "install breaks the driver instead of helping it",
            )

    def test_the_driver_setup_sets_binary_location_only_when_one_was_found(self):
        # The driver kwargs are built in _launch_browser(), which login() calls.
        source = inspect.getsource(GarminClient._launch_browser)
        self.assertIn("chromium = _chromium_binary()", source)
        self.assertIn('driver_kwargs["binary_location"] = chromium', source)
        self.assertNotIn(
            'binary_location="/snap/bin/chromium"',
            source,
            "Regression: the snap path is hard-coded again, which breaks every "
            "machine that is not the Oracle VM.",
        )
