"""Focused display-renderer tests for Stage 6.3 — Shell-escaped display.

Tests ``render_command_display``.  No Docker, no network, no subprocess.
The display function produces a POSIX-safe shell representation that is
exactly ``shlex.join()``::

    render_command_display(argv) == shlex.join(list(argv))

``shlex.join`` uses single-quote semantics — safe against accidental
``$HOME`` expansion, backtick execution, or command substitution when
the displayed command is copied into a shell.
"""

from __future__ import annotations

import shlex
import unittest

from docker.versioning.rendering import render_command_display


def _assert_join(test: unittest.TestCase, argv: tuple[str, ...]) -> None:
    """Assert display is exactly shlex.join and round-trips through split."""
    display = render_command_display(argv)
    test.assertIsInstance(display, str)
    expected = shlex.join(list(argv))
    test.assertEqual(
        display,
        expected,
        f"display must be shlex.join: {argv!r}\n"
        f"  got:      {display!r}\n"
        f"  expected: {expected!r}",
    )
    # Round-trip sanity check.
    test.assertEqual(shlex.split(display), list(argv))


# ---------------------------------------------------------------------------
# 6.3  Shell-escaped display tests
# ---------------------------------------------------------------------------


class TestPlainArguments(unittest.TestCase):
    """Plain arguments pass through unchanged."""

    def test_single_word(self):
        _assert_join(self, ("docker", "build", "."))

    def test_multiple_args(self):
        _assert_join(
            self,
            ("docker", "run", "--rm", "--name", "pi-1", "pi-cli-pi:latest"),
        )

    def test_empty_argv(self):
        result = render_command_display(())
        self.assertIsInstance(result, str)
        self.assertEqual(result, shlex.join([]))
        self.assertEqual(result, "")


class TestSpaces(unittest.TestCase):
    """Arguments containing spaces are single-quoted by shlex.join."""

    def test_single_arg_with_space(self):
        _assert_join(self, ("echo", "hello world"))

    def test_path_with_spaces(self):
        _assert_join(self, ("docker", "build", "/home/dev/my project"))

    def test_multiple_args_with_spaces(self):
        _assert_join(self, ("echo", "hello world", "foo bar"))

    def test_trailing_space(self):
        _assert_join(self, ("cmd", "trailing "))

    def test_leading_space(self):
        _assert_join(self, ("cmd", " leading"))


class TestQuotes(unittest.TestCase):
    """Arguments containing quotes are safely escaped by shlex.join."""

    def test_double_quote_inside(self):
        _assert_join(self, ("bash", "-c", 'echo "hello"'))

    def test_single_quote_inside(self):
        _assert_join(self, ("bash", "-c", "echo 'hello'"))

    def test_both_quotes(self):
        _assert_join(self, ("python", "-c", """print('it\\'s "done"')"""))


class TestShellMetacharacters(unittest.TestCase):
    """shlex.join single-quotes $, ;, |, &, <, >, and backticks so that
    a copy-pasted command never triggers shell expansion or execution."""

    def test_dollar_sign(self):
        _assert_join(self, ("bash", "-c", "echo $HOME"))

    def test_semicolon(self):
        _assert_join(self, ("sh", "-c", "echo a; echo b"))

    def test_pipe(self):
        _assert_join(self, ("sh", "-c", "cat file | grep x"))

    def test_backtick(self):
        _assert_join(self, ("sh", "-c", "echo `date`"))

    def test_ampersand(self):
        _assert_join(self, ("sh", "-c", "sleep 10 &"))

    def test_redirect(self):
        _assert_join(self, ("sh", "-c", "cat < in > out"))


class TestEmptyAndEdgeCases(unittest.TestCase):
    """Edge cases: empty string arg, leading-dash arg, spaces-only arg."""

    def test_empty_string_arg(self):
        _assert_join(self, ("cmd", "", "arg"))

    def test_arg_with_leading_dash(self):
        _assert_join(self, ("cmd", "--not-a-flag"))

    def test_arg_with_only_spaces(self):
        _assert_join(self, ("cmd", "   "))


class TestReturnsStringNotEligibleForSubprocess(unittest.TestCase):
    """The display function returns a str — never a tuple or list —
    and must not be plain ``' '.join(argv)`` for metacharacter inputs."""

    def test_return_type_is_str(self):
        result = render_command_display(("docker", "build", "."))
        self.assertIsInstance(result, str)

    def test_display_differs_from_plain_concat(self):
        """The display string must not be plain ``' '.join(argv)`` for
        inputs that contain metacharacters — it must escape them."""
        argv = ("bash", "-c", "echo $HOME")
        plain = " ".join(argv)
        display = render_command_display(argv)
        self.assertNotEqual(
            display,
            plain,
            "display must escape metacharacters, not just join with spaces",
        )

    def test_display_not_fed_to_subprocess(self):
        """``render_command_display`` returns a ``str`` — it MUST NOT
        be fed to ``subprocess.run`` as a command vector.  The execution
        path uses the ``tuple[str, ...]``, never the display string."""
        argv = ("bash", "-c", "echo 'hello world'")
        display = render_command_display(argv)
        self.assertIsInstance(display, str)
        self.assertNotIsInstance(display, (list, tuple))
        # The display is NOT the same as the execution tuple.
        self.assertNotEqual(display, argv)


class TestArtifactMountDisplay(unittest.TestCase):
    """Artifact mount ``--mount`` options with spaces or shell
    metacharacters in host paths are safely single-quoted by
    ``shlex.join`` so a copy-pasted dry-run line never triggers
    unintended expansion or command injection."""

    def test_spaces_in_host_path_quoted(self):
        argv = (
            "docker",
            "run",
            "--rm",
            "--mount",
            "type=bind,src=/home/alice/my projects/.pi-cache/a.tgz,"
            "dst=/run/pi-cli/runtime-artifacts/sha512/abc.tgz,readonly",
            "pi-cli-pi:latest",
        )
        display = render_command_display(argv)
        # The display must single-quote the mount options string
        # because src contains a space.
        self.assertIn(
            "'type=bind,src=/home/alice/my projects/",
            display,
            "mount option containing spaces must be quoted",
        )
        # Round-trip check.
        self.assertEqual(shlex.split(display), list(argv))

    def test_dollar_in_path_quoted(self):
        argv = (
            "docker",
            "run",
            "--mount",
            "type=bind,src=/home/alice/$PROJ/cache/a.tgz,"
            "dst=/run/pi-cli/runtime-artifacts/sha512/abc.tgz,readonly",
            "pi-cli-pi:latest",
        )
        display = render_command_display(argv)
        # $PROJ must be single-quoted so a shell never expands it.
        self.assertIn("'type=bind,src=/home/alice/$PROJ/", display)
        self.assertEqual(shlex.split(display), list(argv))

    def test_multiple_artifact_mounts_with_spaces(self):
        argv = (
            "docker",
            "run",
            "--mount",
            "type=bind,src=/home/alice/my cache/a.tgz,"
            "dst=/run/pi-cli/runtime-artifacts/sha512/a.tgz,readonly",
            "--mount",
            "type=bind,src=/home/alice/my cache/b.tgz,"
            "dst=/run/pi-cli/runtime-artifacts/sha256/b.tgz,readonly",
            "pi-cli-pi:latest",
        )
        display = render_command_display(argv)
        # Both mount options must be individually quoted.
        parts = shlex.split(display)
        self.assertEqual(parts, list(argv))
        # Count quoted mount options.
        self.assertEqual(display.count("'type=bind,src="), 2)
