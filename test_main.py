import io
import sys
import unittest
from unittest.mock import Mock, patch


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


class TestCli(unittest.TestCase):
    def run_lifecycle(self, *, desktop=False, interactive=False, fatal=True, exit_code=0):
        from cooler_btop import main

        class LifecycleApp(main.BtopCloneApp):
            def on_mount(self):
                if fatal:
                    raise RuntimeError("lifecycle mount failed")
                self.exit(return_code=exit_code)

            def run(self):
                return super().run(headless=True)

        stdin = (TerminalInput if interactive else io.StringIO)("\nunused input\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["cooler-btop"] + (["--desktop"] if desktop else [])
        with patch.object(sys, "argv", argv), \
                patch.object(main, "BtopCloneApp", LifecycleApp), \
                patch.object(sys, "stdin", stdin), \
                patch.object(sys, "stdout", stdout), \
                patch.object(sys, "stderr", stderr):
            status = main.run_cli()
        return status, stdout.getvalue(), stderr.getvalue(), stdin.read()

    def test_fatal_lifecycle_reports_cli_error_and_restores_terminal(self):
        status, stdout, stderr, _ = self.run_lifecycle()
        self.assertEqual(status, 1)
        self.assertIn("Cooler btop could not start.\nRuntimeError: lifecycle mount failed", stderr)
        self.assertIn("https://github.com/l0ee/cooler-btop/issues", stderr)
        self.assertIn("\033[?25h\033[?1049l", stdout)

    def test_interactive_desktop_fatal_lifecycle_waits_for_enter(self):
        status, _, stderr, remaining = self.run_lifecycle(desktop=True, interactive=True)
        self.assertEqual(remaining, "unused input\n")
        self.assertEqual(status, 1)
        self.assertIn("Press Enter", stderr)

    def test_noninteractive_desktop_fatal_lifecycle_does_not_wait(self):
        status, _, stderr, remaining = self.run_lifecycle(desktop=True)
        self.assertEqual(status, 1)
        self.assertEqual(remaining, "\nunused input\n")
        self.assertNotIn("Press Enter", stderr)

    def test_normal_cli_fatal_lifecycle_does_not_wait(self):
        status, _, stderr, remaining = self.run_lifecycle(interactive=True)
        self.assertEqual(status, 1)
        self.assertEqual(remaining, "\nunused input\n")
        self.assertNotIn("Press Enter", stderr)

    def test_successful_lifecycle_returns_zero_without_waiting(self):
        status, _, stderr, remaining = self.run_lifecycle(
            desktop=True, interactive=True, fatal=False,
        )
        self.assertEqual(status, 0)
        self.assertEqual(remaining, "\nunused input\n")
        self.assertEqual(stderr, "")

    def test_nonzero_lifecycle_exit_without_exception_reports_status(self):
        status, stdout, stderr, _ = self.run_lifecycle(fatal=False, exit_code=2)
        self.assertNotEqual(status, 0)
        self.assertIn("status 2", stderr)
        self.assertIn("https://github.com/l0ee/cooler-btop/issues", stderr)
        self.assertIn("\033[?25h\033[?1049l", stdout)

    def test_desktop_failure_stays_visible_and_returns_nonzero(self):
        from cooler_btop import main
        stdin = TerminalInput("\nunused input\n")
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["cooler-btop", "--desktop"]), \
                patch.object(main, "BtopCloneApp", side_effect=RuntimeError("display failed")), \
                patch.object(main.sys, "stdin", stdin), \
                patch.object(main.sys, "stdout", io.StringIO()), \
                patch.object(main.sys, "stderr", stderr):
            self.assertEqual(main.run_cli(), 1)
        self.assertEqual(stdin.read(), "unused input\n")
        self.assertIn("RuntimeError: display failed", stderr.getvalue())
        self.assertIn("github.com/l0ee/cooler-btop/issues", stderr.getvalue())

    def test_terminal_failure_does_not_wait_for_input(self):
        from cooler_btop import main
        app = Mock()
        app.run.side_effect = RuntimeError("display failed")
        stdin = TerminalInput("\nunused input\n")
        with patch.object(sys, "argv", ["cooler-btop"]), \
                patch.object(main, "BtopCloneApp", return_value=app), \
                patch.object(main.sys, "stdin", stdin), \
                patch.object(main.sys, "stdout", io.StringIO()), \
                patch.object(main.sys, "stderr", io.StringIO()):
            self.assertEqual(main.run_cli(), 1)
        self.assertEqual(stdin.read(), "\nunused input\n")

    def test_daemon_passes_loopback_host_by_default(self):
        with patch.object(sys, 'argv', ['cooler-btop', '--daemon']), \
                patch('cooler_btop.server.run_server', return_value=0) as run_server:
            from cooler_btop.main import run_cli

            self.assertEqual(run_cli(), 0)
        run_server.assert_called_once_with(
            host='127.0.0.1', port=8080, db_path=None, interval=1.0,
        )

    def test_status_returns_nonzero_when_daemon_is_unavailable(self):
        with patch.object(sys, 'argv', ['cooler-btop', '--status']), \
                patch('requests.get', side_effect=OSError('offline')):
            from cooler_btop.main import run_cli

            self.assertNotEqual(run_cli(), 0)


if __name__ == '__main__':
    unittest.main()
