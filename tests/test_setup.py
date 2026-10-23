from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from playwright.sync_api import Error as BrowserError

from lead_data import read_clean_csv
from linkedin_browser import LinkedInBrowser
from outreach import ProcessLock, StopRun, browser_session, main, profile_directory
from outreach_config import load_settings, state_path, template
from outreach_setup import prepare_source, worker_running
from outreach_errors import AccountBlocked


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.csv = self.root / "my contacts.csv"
        self.csv.write_text("name,company_name,city,linkedin_url\nMorgan Sample,Example Company,Boston,https://linkedin.com/in/morgan-example/\n", encoding="utf-8")
        self.settings = self.root / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, **values):
        self.settings.write_text(json.dumps({"csv_file": self.csv.name, **values}), encoding="utf-8")
        return load_settings(self.settings)

    def cli(self, *args):
        if args and args[0] == 'init':
            args = (*args, '--state-directory', str(self.root / '.local'))
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = main(["--settings", str(self.settings), *args])
        return result, output.getvalue()

    def test_fresh_setup_preview_status_and_original_is_unchanged(self):
        before = self.csv.read_bytes()
        code, output = self.cli("init", "--csv", str(self.csv), "--browser", "brave")
        self.assertEqual(code, 0, output)
        cfg = load_settings(self.settings)
        self.assertTrue(cfg["headless"])
        self.assertEqual(cfg["browser_channel"], "brave")
        self.assertEqual(cfg["campaign_invitation_limit"], 1)
        self.assertTrue((self.root / "messages.json").exists())
        with patch("outreach.browser_session", side_effect=AssertionError("Offline preview launched a browser")):
            code, output = self.cli("preview", "--limit", "1")
        self.assertEqual(code, 0, output)
        self.assertIn("Hi Morgan", output)
        self.assertEqual(self.csv.read_bytes(), before)
        code, output = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("stopped", output)
        self.assertEqual(self.cli("init", "--csv", str(self.csv))[0], 1)

    def test_custom_messages_support_coach_placeholders(self):
        path = self.root / "custom messages.json"
        messages = {"connection_note": "Hi {{FirstName}}, connect in {{location}}?",
                    "message_1": "Thanks {{FirstName}}. How is {{CompanyName}}?",
                    "message_2": "Can we chat about {company}?"}
        path.write_text(json.dumps(messages), encoding="utf-8")
        cfg = self.config(message_templates_file=path.name)
        self.assertEqual(cfg["message_1"], "Thanks {first_name}. How is {company}?")
        self.assertEqual(cfg["connection_note"], "Hi {first_name}, connect in {location}?")

    def test_invalid_settings_fail_before_browser_or_state_mutation(self):
        for values in ({"headless": "false"}, {"invitations_per_day": True}, {"working_hours": [18, 9]},
                       {"unknown_setting": 1}, {"message_1": "Hi {first_name.__class__}"},
                       {"message_1": "Hi {first_name!r}"}, {"working_days": []}):
            with self.subTest(values=values), self.assertRaises((ValueError, KeyError)):
                self.config(**values)

    def test_total_cap_can_be_disabled_but_rejects_invalid_values(self):
        self.assertIsNone(self.config(campaign_invitation_limit=None)["campaign_invitation_limit"])
        for value in (0, -1, True, "unlimited"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.config(campaign_invitation_limit=value)

    def test_retry_bounds_are_validated_before_worker_start(self):
        for values in ({'read_attempts': 6}, {'contact_retry_limit': 0},
                       {'retry_initial_seconds': 300, 'retry_max_seconds': 60}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.config(**values)

    def test_missing_browser_executable_is_actionable_instead_of_retried_forever(self):
        fake = MagicMock()
        fake.chromium.launch_persistent_context.side_effect = BrowserError("Executable doesn't exist at missing-browser")
        with patch('linkedin_browser.sync_playwright') as factory:
            factory.return_value.start.return_value = fake
            with self.assertRaises(StopRun):
                with LinkedInBrowser(self.root / 'profile'):
                    pass
        fake.stop.assert_called_once()

    def test_browser_cleanup_error_does_not_hide_an_account_block(self):
        adapter = LinkedInBrowser(self.root / 'profile')
        adapter.context, adapter.pw = MagicMock(), MagicMock()
        adapter.context.close.side_effect = BrowserError('Browser already closed')
        with self.assertRaises(AccountBlocked):
            try:
                raise AccountBlocked('Weekly limit')
            except AccountBlocked as error:
                adapter.__exit__(type(error), error, error.__traceback__)
                raise
        adapter.pw.stop.assert_called_once()

    def test_browser_cleanup_error_without_another_error_is_recoverable(self):
        adapter = LinkedInBrowser(self.root / 'profile')
        adapter.context, adapter.pw = MagicMock(), MagicMock()
        adapter.context.close.side_effect = BrowserError('Browser transport lost')
        with self.assertRaises(BrowserError):
            adapter.__exit__(None, None, None)
        adapter.pw.stop.assert_called_once()

    def test_background_start_requires_explicit_live_flag(self):
        self.config()
        with patch("outreach_setup.subprocess.Popen") as launch:
            code, output = self.cli("start")
        self.assertEqual(code, 1)
        self.assertIn("requires --live", output)
        launch.assert_not_called()

    def test_stop_still_works_when_message_file_is_missing(self):
        self.settings.write_text(json.dumps({"state_directory": "private/state", "message_templates_file": "missing.json"}), encoding="utf-8")
        code, output = self.cli("stop-watch")
        self.assertEqual(code, 0, output)
        self.assertTrue((self.root / "private/state/stop-watch.request").is_file())

    def test_lock_prevents_parallel_workers(self):
        directory = self.root / "state"
        self.assertFalse(worker_running(directory))
        with ProcessLock(directory / "process.lock"):
            self.assertTrue(worker_running(directory))
        self.assertFalse(worker_running(directory))

    def test_browser_defaults_headless_but_login_mode_is_visible(self):
        cfg = self.config(browser_channel="brave")
        self.assertTrue(browser_session(cfg).headless)
        self.assertFalse(browser_session(cfg, visible=True).headless)
        self.assertEqual(browser_session(cfg).channel, "brave")
        with self.assertRaises(StopRun):
            LinkedInBrowser(self.root, headless=True).login()

    def test_launch_options_really_use_headless_not_minimized_windows(self):
        fake = MagicMock()
        fake.chromium.launch_persistent_context.return_value.pages = []
        with patch("linkedin_browser.sync_playwright") as factory:
            factory.return_value.start.return_value = fake
            with LinkedInBrowser(self.root / "profile", headless=True):
                pass
        options = fake.chromium.launch_persistent_context.call_args.kwargs
        self.assertTrue(options["headless"])
        self.assertTrue(options["chromium_sandbox"])
        self.assertEqual(options["channel"], "chromium")

    def test_state_and_csv_paths_are_relative_to_settings_not_cwd(self):
        cfg = self.config(state_directory="private/state")
        self.assertEqual(state_path(cfg), self.root / "private/state")
        directory = state_path(cfg)
        directory.mkdir(parents=True)
        path, report = prepare_source(cfg, directory)
        _, rows, _ = read_clean_csv(path)
        self.assertEqual(rows[0]["First Name"], "Morgan")
        self.assertEqual(report["input_rows"], 1)
        first = path.stat().st_mtime_ns
        prepare_source(cfg, directory)
        self.assertEqual(path.stat().st_mtime_ns, first)

    def test_csv_header_aliases_semicolon_and_split_names(self):
        self.csv.write_text('first_name;last_name;company_name;profile_url\nMorgan;Sample;Example Company;https://linkedin.com/in/morgan-example/\n', encoding="utf-8")
        headers, rows, _ = read_clean_csv(self.csv)
        self.assertIn("Full Name", headers)
        self.assertEqual(rows[0]["Full Name"], "Morgan Sample")

    def test_explicit_profile_path_and_template_errors(self):
        self.assertEqual(profile_directory("brave", self.root, "private/profile"), self.root / "private/profile")
        for value in ("Hi {{unknown}}", "Hi {first_name:>100}"):
            with self.assertRaises(ValueError):
                template(value)


if __name__ == "__main__":
    unittest.main()
