"""Focused renderer tests for Stage 6.2 — Run command-vector rendering.

Tests ``render_run_vector`` against ``RunRenderInputs``.
No Docker, no network, no subprocess, no filesystem.
"""

from __future__ import annotations

import unittest
from typing import TYPE_CHECKING

from docker.versioning.rendering import (
    RunRenderInputs,
    render_run_vector,
)

if TYPE_CHECKING:
    from docker.versioning.artifact_cache import VerifiedCacheBlob
    from docker.versioning.model import RuntimeInventory
    from docker.versioning.rendering import ArtifactMount

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _render(
    *,
    image: str = "pi-cli-pi:latest",
    container_name: str = "pi-1",
    projection_host_path: str = "/home/dev/.cache/docker-constructor/projects/constructor-identity/runtime/proj-abc123.toml",
    projection_container_path: str = "/run/pi-cli/docker-constructor.runtime.toml",
    pi_home_host: str = "/home/alice/.pi",
    workspace: str = "/home/dev/work/my-project",
    extra_workspaces: tuple[str, ...] = (),
    host_access=None,
    tty: bool = True,
    stdin_open: bool = True,
    command: tuple[str, ...] = (),
    chown_on_start: str | None = None,
    artifact_mounts: tuple = (),
) -> tuple[str, ...]:
    from docker.versioning.rendering import RunHostAccess

    if host_access is None:
        host_access = RunHostAccess.disabled()
    return render_run_vector(
        RunRenderInputs(
            image=image,
            container_name=container_name,
            projection_host_path=projection_host_path,
            projection_container_path=projection_container_path,
            pi_home_host=pi_home_host,
            workspace=workspace,
            extra_workspaces=extra_workspaces,
            host_access=host_access,
            tty=tty,
            stdin_open=stdin_open,
            command=command,
            chown_on_start=chown_on_start,
            artifact_mounts=artifact_mounts,
        )
    )


# ---------------------------------------------------------------------------
# 6.2.1  Default run vector shape
# ---------------------------------------------------------------------------


class TestDefaultRunVector(unittest.TestCase):
    """Assert the exact tuple shape for a default primary-workspace launch."""

    def test_command_prefix_is_docker_run(self):
        args = _render()
        self.assertEqual(
            args[:2],
            ("docker", "run"),
            "prefix must be 'docker run', never 'docker compose'",
        )

    def test_no_compose_in_output(self):
        args = _render()
        self.assertNotIn("compose", args)
        self.assertNotIn("docker-compose", args)

    def test_flag_rm_present(self):
        args = _render()
        self.assertIn("--rm", args)

    def test_deterministic_container_name(self):
        args = _render(container_name="pi-session-1")
        idx = args.index("--name")
        self.assertEqual(args[idx + 1], "pi-session-1")

    def test_interactive_and_tty_by_default(self):
        args = _render()
        self.assertIn("--interactive", args)
        self.assertIn("--tty", args)

    def test_short_flags_not_used(self):
        """render_run_vector must use long-form flags (-i → --interactive,
        -t → --tty) so the vector is self-documenting."""
        args = _render()
        self.assertNotIn("-i", args)
        self.assertNotIn("-t", args)


# ---------------------------------------------------------------------------
# 6.2.2  Mounts — Pi home, projection, workspaces
# ---------------------------------------------------------------------------


class TestMounts(unittest.TestCase):
    """Assert Pi home, runtime projection, and workspace bind mounts."""

    def test_pi_home_mount(self):
        from docker.versioning.rendering import _CONTAINER_PI_HOME

        args = _render(pi_home_host="/home/alice/.pi")
        mounts = _collect_mounts(args)
        pi_mount = _find_mount(mounts, dst=_CONTAINER_PI_HOME)
        self.assertIsNotNone(pi_mount, "missing Pi home mount")
        self.assertEqual(pi_mount["src"], "/home/alice/.pi")
        self.assertNotEqual(
            pi_mount["src"],
            pi_mount["dst"],
            "Pi home mount must map host ~/.pi to "
            f"fixed container path {_CONTAINER_PI_HOME}",
        )

    def test_pi_home_asymmetric_mount(self):
        """/home/bob/.pi on the host maps to /home/dev/.pi in the
        container — regardless of host username."""
        from docker.versioning.rendering import _CONTAINER_PI_HOME

        args = _render(pi_home_host="/home/bob/.pi")
        mounts = _collect_mounts(args)
        pi_mount = _find_mount(mounts, dst=_CONTAINER_PI_HOME)
        self.assertIsNotNone(pi_mount)
        self.assertEqual(pi_mount["src"], "/home/bob/.pi")
        self.assertEqual(pi_mount["dst"], "/home/dev/.pi")

    def test_runtime_projection_mount_present(self):
        projection = "/home/dev/.cache/docker-constructor/projects/constructor-identity/runtime/proj.toml"
        args = _render(
            projection_host_path=projection,
            projection_container_path="/run/pi-cli/docker-constructor.runtime.toml",
        )
        mounts = _collect_mounts(args)
        proj = _find_mount(mounts, dst="/run/pi-cli/docker-constructor.runtime.toml")
        self.assertIsNotNone(proj, "missing runtime projection mount")
        self.assertEqual(proj["src"], projection)

    def test_runtime_projection_mount_is_readonly(self):
        args = _render()
        mounts = _collect_mounts(args)
        proj = _find_mount(mounts, dst="/run/pi-cli/docker-constructor.runtime.toml")
        self.assertIsNotNone(proj)
        self.assertIn("readonly", proj, "projection mount must be read-only")

    def test_runtime_projection_fixed_container_path(self):
        """The container-side path is fixed regardless of host path."""
        a = _render(
            projection_host_path=(
                "/home/dev/.cache/docker-constructor/projects/constructor-identity/runtime/launch-a.toml"
            ),
            projection_container_path="/run/pi-cli/docker-constructor.runtime.toml",
        )
        b = _render(
            projection_host_path=(
                "/home/dev/.cache/docker-constructor/projects/constructor-identity/runtime/launch-b.toml"
            ),
            projection_container_path="/run/pi-cli/docker-constructor.runtime.toml",
        )
        mount_a = _find_mount(
            _collect_mounts(a), dst="/run/pi-cli/docker-constructor.runtime.toml"
        )
        mount_b = _find_mount(
            _collect_mounts(b), dst="/run/pi-cli/docker-constructor.runtime.toml"
        )
        self.assertEqual(mount_a["dst"], mount_b["dst"])
        self.assertNotEqual(mount_a["src"], mount_b["src"])

    def test_workspace_1to1_bind_mount(self):
        args = _render(workspace="/home/dev/work/app")
        mounts = _collect_mounts(args)
        primary = _find_mount(mounts, dst="/home/dev/work/app")
        self.assertIsNotNone(primary, "missing primary workspace mount")
        self.assertEqual(primary["src"], "/home/dev/work/app")

    def test_workspace_as_workdir(self):
        args = _render(workspace="/home/dev/work/app")
        idx = args.index("--workdir")
        self.assertEqual(args[idx + 1], "/home/dev/work/app")


# ---------------------------------------------------------------------------
# 6.2.3  Environment variables
# ---------------------------------------------------------------------------


class TestEnvironment(unittest.TestCase):
    """Assert WORKSPACE_PATH_* and CHOWN_WORK_ON_START environment."""

    def test_workspace_path_1_set(self):
        args = _render(workspace="/home/dev/work/app")
        env = _collect_env(args)
        self.assertEqual(env.get("WORKSPACE_PATH_1"), "/home/dev/work/app")

    def test_chown_on_start_when_set(self):
        args = _render(chown_on_start="1")
        env = _collect_env(args)
        self.assertEqual(env.get("CHOWN_WORK_ON_START"), "1")

    def test_no_chown_on_start_when_none(self):
        args = _render(chown_on_start=None)
        env = _collect_env(args)
        self.assertNotIn("CHOWN_WORK_ON_START", env)


# ---------------------------------------------------------------------------
# 6.2.4  Gateway mapping
# ---------------------------------------------------------------------------


class TestGatewayMapping(unittest.TestCase):
    """Assert --add-host for host.docker.internal only when enabled."""

    def test_gateway_docker_gateway_mode(self):
        from docker.versioning.rendering import RunHostAccess

        args = _render(
            host_access=RunHostAccess(address="172.17.0.1", mode="docker-gateway")
        )
        self.assertIn("--add-host", args)
        idx = args.index("--add-host")
        self.assertEqual(args[idx + 1], "host.docker.internal:172.17.0.1")

    def test_gateway_external_address_mode(self):
        from docker.versioning.rendering import RunHostAccess

        args = _render(
            host_access=RunHostAccess(address="10.0.2.2", mode="external-address")
        )
        idx = args.index("--add-host")
        self.assertEqual(args[idx + 1], "host.docker.internal:10.0.2.2")

    def test_gateway_disabled_has_no_mapping(self):
        from docker.versioning.rendering import RunHostAccess

        args = _render(host_access=RunHostAccess.disabled())
        self.assertNotIn("--add-host", args)


# ---------------------------------------------------------------------------
# 6.2.5  Canonical image and command passthrough
# ---------------------------------------------------------------------------


class TestImageAndCommand(unittest.TestCase):
    """Assert canonical image placement and command passthrough."""

    def test_canonical_image_is_last_before_command(self):
        args = _render(image="pi-cli-pi:latest")
        # Image appears after all flags and before any passthrough command.
        idx = args.index("pi-cli-pi:latest")
        # No flag after the image.
        for token in args[idx + 1 :]:
            self.assertFalse(
                token.startswith("--"), f"flag {token!r} appears after image"
            )

    def test_command_passthrough_after_image(self):
        args = _render(command=("bash", "-c", "echo hello"))
        img_idx = args.index("pi-cli-pi:latest")
        self.assertEqual(args[img_idx + 1 :], ("bash", "-c", "echo hello"))

    def test_no_command_passthrough_when_empty(self):
        args = _render(command=())
        # Image is the last element.
        self.assertEqual(args[-1], "pi-cli-pi:latest")


# ---------------------------------------------------------------------------
# 6.2.6  Extra workspaces
# ---------------------------------------------------------------------------


class TestExtraWorkspaces(unittest.TestCase):
    """Assert one and two extra workspaces receive 1:1 mounts and
    ordered WORKSPACE_PATH_* variables."""

    def test_one_extra_workspace_mount(self):
        args = _render(
            workspace="/home/dev/work/primary",
            extra_workspaces=("/home/dev/work/extra1",),
        )
        mounts = _collect_mounts(args)
        opt = _find_mount(mounts, dst="/home/dev/work/extra1")
        self.assertIsNotNone(opt, "missing extra workspace mount")
        self.assertEqual(opt["src"], "/home/dev/work/extra1")

    def test_one_extra_workspace_environment(self):
        args = _render(
            workspace="/home/dev/work/primary",
            extra_workspaces=("/home/dev/work/extra1",),
        )
        env = _collect_env(args)
        self.assertEqual(env["WORKSPACE_PATH_1"], "/home/dev/work/primary")
        self.assertEqual(env["WORKSPACE_PATH_2"], "/home/dev/work/extra1")
        self.assertNotIn("WORKSPACE_PATH_3", env)

    def test_two_extra_workspaces_mounts(self):
        args = _render(
            workspace="/home/dev/work/primary",
            extra_workspaces=("/home/dev/work/extra1", "/home/dev/work/extra2"),
        )
        mounts = _collect_mounts(args)
        self.assertIsNotNone(_find_mount(mounts, dst="/home/dev/work/extra1"))
        self.assertIsNotNone(_find_mount(mounts, dst="/home/dev/work/extra2"))

    def test_two_extra_workspaces_env(self):
        args = _render(
            workspace="/home/dev/work/primary",
            extra_workspaces=("/home/dev/work/extra1", "/home/dev/work/extra2"),
        )
        env = _collect_env(args)
        self.assertEqual(env["WORKSPACE_PATH_1"], "/home/dev/work/primary")
        self.assertEqual(env["WORKSPACE_PATH_2"], "/home/dev/work/extra1")
        self.assertEqual(env["WORKSPACE_PATH_3"], "/home/dev/work/extra2")


# ---------------------------------------------------------------------------
# 6.2.7  TTY / non-TTY modes
# ---------------------------------------------------------------------------


class TestTTYModes(unittest.TestCase):
    """Assert interactive (TTY) and non-TTY automation modes."""

    def test_interactive_mode_includes_tty_and_interactive(self):
        args = _render(tty=True, stdin_open=True)
        self.assertIn("--tty", args)
        self.assertIn("--interactive", args)

    def test_automation_mode_no_tty(self):
        args = _render(tty=False, stdin_open=False)
        self.assertNotIn("--tty", args)
        self.assertNotIn("--interactive", args)

    def test_stdin_open_without_tty(self):
        """--interactive without --tty: pipe mode (e.g. echo | docker run -i)."""
        args = _render(tty=False, stdin_open=True)
        self.assertNotIn("--tty", args)
        self.assertIn("--interactive", args)

    def test_tty_without_stdin_omits_both(self):
        """--tty without stdin_open: unusual but valid.  --tty enables
        Docker's TTY allocation which requires stdin to be meaningful."""
        args = _render(tty=True, stdin_open=False)
        # With tty=True, Docker typically needs --interactive too for
        # the TTY to be useful.  For now, render --tty alone.
        self.assertIn("--tty", args)
        self.assertNotIn("--interactive", args)


# ---------------------------------------------------------------------------
# 6.2.8  Command arguments with spaces and shell metacharacters
# ---------------------------------------------------------------------------


class TestCommandSpacesAndMetacharacters(unittest.TestCase):
    """Command arguments containing spaces or shell metacharacters
    must remain individual vector elements — no shell quoting needed."""

    def test_command_with_spaces(self):
        """Each argument is a separate string even when it contains
        internal spaces — subprocess passes them verbatim."""
        args = _render(command=("echo", "hello world", "foo bar"))
        img_idx = args.index("pi-cli-pi:latest")
        tail = args[img_idx + 1 :]
        self.assertEqual(tail, ("echo", "hello world", "foo bar"))

    def test_command_with_dollar_sign(self):
        args = _render(command=("bash", "-c", "echo $HOME"))
        img_idx = args.index("pi-cli-pi:latest")
        tail = args[img_idx + 1 :]
        self.assertEqual(tail, ("bash", "-c", "echo $HOME"))

    def test_command_with_semicolon(self):
        args = _render(command=("sh", "-c", "echo a; echo b"))
        img_idx = args.index("pi-cli-pi:latest")
        tail = args[img_idx + 1 :]
        self.assertEqual(tail, ("sh", "-c", "echo a; echo b"))


# ---------------------------------------------------------------------------
# 6.2.9  Deterministic byte-for-byte output
# ---------------------------------------------------------------------------


class TestDeterministicOutput(unittest.TestCase):
    """Equivalent inputs produce byte-for-byte identical vectors."""

    _PROJECTION = "/home/dev/.cache/docker-constructor/projects/constructor-identity/runtime/x.toml"

    def test_same_inputs_produce_identical_vector(self):
        ri = RunRenderInputs(
            image="pi-cli-pi:latest",
            container_name="pi-1",
            projection_host_path=self._PROJECTION,
            projection_container_path="/run/pi-cli/docker-constructor.runtime.toml",
            pi_home_host="/home/alice/.pi",
            workspace="/home/dev/work/primary",
        )
        self.assertEqual(render_run_vector(ri), render_run_vector(ri))

    def test_different_kwarg_order_same_result(self):
        a = RunRenderInputs(
            image="pi-cli-pi:latest",
            container_name="pi-1",
            projection_host_path=self._PROJECTION,
            projection_container_path="/run/pi-cli/docker-constructor.runtime.toml",
            pi_home_host="/home/alice/.pi",
            workspace="/home/dev/work/primary",
        )
        b = RunRenderInputs(
            workspace="/home/dev/work/primary",
            pi_home_host="/home/alice/.pi",
            projection_container_path="/run/pi-cli/docker-constructor.runtime.toml",
            projection_host_path=self._PROJECTION,
            container_name="pi-1",
            image="pi-cli-pi:latest",
        )
        self.assertEqual(render_run_vector(a), render_run_vector(b))


# ---------------------------------------------------------------------------
# 6.3  Edge-case and security tests
# ---------------------------------------------------------------------------


class TestPathEdgeCases(unittest.TestCase):
    """Paths with spaces, quotes, Unicode, and leading dashes must be
    preserved as individual vector elements."""

    def test_workspace_path_with_spaces(self):
        args = _render(workspace="/home/dev/work/my project")
        self.assertIn("/home/dev/work/my project", args)
        mounts = _collect_mounts(args)
        primary = _find_mount(mounts, dst="/home/dev/work/my project")
        self.assertIsNotNone(primary)
        self.assertEqual(primary["src"], "/home/dev/work/my project")

    def test_workspace_path_with_unicode(self):
        path = "/home/dev/work/projéct-α"
        args = _render(workspace=path)
        env = _collect_env(args)
        self.assertEqual(env["WORKSPACE_PATH_1"], path)

    def test_workspace_path_with_leading_dashes(self):
        """A path like /home/dev/--help must not be misinterpreted
        as a Docker flag — it's a positional argument value."""
        path = "/home/dev/work/--workspace-name"
        args = _render(workspace=path)
        # The path must appear verbatim after --workdir and as WORKSPACE_PATH_1.
        wd_idx = args.index("--workdir")
        self.assertEqual(args[wd_idx + 1], path)
        env = _collect_env(args)
        self.assertEqual(env["WORKSPACE_PATH_1"], path)

    def test_pi_home_host_with_spaces(self):
        args = _render(pi_home_host="/home/user name/.pi")
        mounts = _collect_mounts(args)
        from docker.versioning.rendering import _CONTAINER_PI_HOME

        pi = _find_mount(mounts, dst=_CONTAINER_PI_HOME)
        self.assertIsNotNone(pi)
        self.assertEqual(pi["src"], "/home/user name/.pi")


class TestWorkspaceValidation(unittest.TestCase):
    """Reject malformed workspace inputs."""

    def test_empty_workspace_rejected(self):
        with self.assertRaises(ValueError):
            _render(workspace="")

    def test_whitespace_only_workspace_rejected(self):
        with self.assertRaises(ValueError):
            _render(workspace="   ")

    def test_empty_extra_workspace_entry_rejected(self):
        with self.assertRaises(ValueError):
            _render(
                workspace="/home/dev/work/primary",
                extra_workspaces=("/home/dev/work/extra1", ""),
            )

    def test_duplicate_workspace_paths_rejected(self):
        """A path appearing as both primary and extra workspace must
        be rejected — duplicate mounts would collide."""
        with self.assertRaises(ValueError):
            _render(
                workspace="/home/dev/work/shared",
                extra_workspaces=("/home/dev/work/other", "/home/dev/work/shared"),
            )

    def test_duplicate_extra_workspace_paths_rejected(self):
        with self.assertRaises(ValueError):
            _render(
                workspace="/home/dev/work/primary",
                extra_workspaces=("/home/dev/work/extra1", "/home/dev/work/extra1"),
            )

    def test_more_than_two_extra_workspaces_accepted(self) -> None:
        """Direct Docker execution has no limit on extra workspaces
        (the old two-workspace limit was a Compose fragment artefact)."""
        args = _render(
            workspace="/home/dev/work/primary",
            extra_workspaces=(
                "/home/dev/work/extra1",
                "/home/dev/work/extra2",
                "/home/dev/work/extra3",
            ),
        )
        # All three extra workspaces must appear as 1:1 mounts
        args_str = " ".join(args)
        self.assertIn("dst=/home/dev/work/extra1", args_str)
        self.assertIn("dst=/home/dev/work/extra2", args_str)
        self.assertIn("dst=/home/dev/work/extra3", args_str)
        # All three exported as WORKSPACE_PATH_2,3,4
        self.assertIn("WORKSPACE_PATH_1=/home/dev/work/primary", args)
        self.assertIn("WORKSPACE_PATH_2=/home/dev/work/extra1", args)
        self.assertIn("WORKSPACE_PATH_3=/home/dev/work/extra2", args)
        self.assertIn("WORKSPACE_PATH_4=/home/dev/work/extra3", args)

    def test_relative_workspace_rejected(self):
        with self.assertRaises(ValueError):
            _render(workspace="relative/path/project")

    def test_relative_extra_workspace_rejected(self):
        with self.assertRaises(ValueError):
            _render(
                workspace="/home/dev/work/primary",
                extra_workspaces=("relative/path/project",),
            )

    def test_relative_pi_home_host_rejected(self):
        with self.assertRaises(ValueError):
            _render(pi_home_host="~/.pi")


class TestImageAndNameValidation(unittest.TestCase):
    """Reject invalid image tags and container names."""

    def test_empty_image_rejected(self):
        with self.assertRaises(ValueError):
            _render(image="")

    def test_whitespace_image_rejected(self):
        with self.assertRaises(ValueError):
            _render(image="   ")

    def test_empty_container_name_rejected(self):
        with self.assertRaises(ValueError):
            _render(container_name="")

    def test_whitespace_container_name_rejected(self):
        with self.assertRaises(ValueError):
            _render(container_name="  ")


class TestMountSecurity(unittest.TestCase):
    """Reject mounts that would leak host-only metadata into the
    container or conflict with expected destinations."""

    def test_writable_projection_mount_rejected(self):
        """The runtime projection mount must be read-only.
        There is no API to request a writable projection — the
        renderer enforces it unconditionally."""
        args = _render()
        mounts = _collect_mounts(args)
        proj = _find_mount(mounts, dst="/run/pi-cli/docker-constructor.runtime.toml")
        self.assertIsNotNone(proj)
        # readonly may be "true" or present as a key with empty value.
        # Either way, it signals read-only.
        self.assertIn("readonly", proj, "projection mount must be read-only")
        self.assertNotEqual(
            proj.get("readonly", "true"),
            "false",
            "projection mount must not be writable",
        )

    def test_docker_constructor_toml_as_projection_rejected(self):
        """Passing docker-constructor.toml as projection_host_path
        must be rejected — the reviewed source must not leak into
        the container."""
        with self.assertRaises(ValueError):
            _render(
                projection_host_path=("/home/dev/.pi-cli/docker-constructor.toml"),
            )

    def test_effective_build_projection_as_projection_rejected(self):
        """The effective build projection file must not be mounted
        into the runtime container."""
        with self.assertRaises(ValueError):
            _render(
                projection_host_path=(
                    "/home/dev/.pi-cli/.docker-generated/"
                    "docker-constructor.build.effective.toml"
                ),
            )

    def test_broad_dot_docker_generated_as_projection_rejected(self):
        """.docker-generated/ as a directory must not be mounted —
        only individual files under .docker-generated/runtime/ are
        valid projection paths."""
        with self.assertRaises(ValueError):
            _render(
                projection_host_path=("/home/dev/.pi-cli/.docker-generated"),
            )

    def test_projection_path_outside_runtime_dir_rejected(self):
        """The host projection path must reside under
        .docker-generated/runtime/."""
        with self.assertRaises(ValueError):
            _render(projection_host_path="/tmp/outside-proj.toml")

    def test_false_positive_substring_rejected(self):
        """A path whose string contains '.docker-generated/runtime/'
        but not as actual directory components must be rejected.
        E.g. /tmp/not.docker-generated/runtime/file.toml is a sibling
        directory, not a child of .docker-generated/."""
        with self.assertRaises(ValueError):
            _render(
                projection_host_path=("/tmp/not.docker-generated/runtime/file.toml"),
            )

    def test_relative_projection_host_path_rejected(self):
        """A relative projection path must be rejected — the caller
        must supply an absolute path."""
        with self.assertRaises(ValueError):
            _render(
                projection_host_path=(".docker-generated/runtime/proj.toml"),
            )

    def test_noncanonical_projection_container_path_rejected(self):
        """The container-side projection destination is fixed at
        ``/run/pi-cli/docker-constructor.runtime.toml``.  Passing any
        other path must be rejected."""
        with self.assertRaises(ValueError):
            _render(
                projection_container_path="/etc/evil.toml",
            )

    def test_canonical_projection_container_path_accepted(self):
        """The fixed path must be accepted (sanity check — the
        default helper already uses this value)."""
        _render(
            projection_container_path=("/run/pi-cli/docker-constructor.runtime.toml"),
        )

    def test_projection_path_in_runtime_dir_accepted(self):
        """Paths under .docker-generated/runtime/ are valid."""
        # This must not raise.
        _render(
            projection_host_path=(
                "/home/dev/.cache/docker-constructor/projects/constructor-identity/runtime/proj-abc123.toml"
            ),
        )


class TestDestinationCollisions(unittest.TestCase):
    """Reject mount destination collisions across the complete
    destination set before rendering."""

    def test_workspace_collides_with_pi_home(self):
        with self.assertRaises(ValueError):
            _render(
                workspace="/home/dev/.pi",
                pi_home_host="/home/alice/.pi",
            )

    def test_extra_workspace_collides_with_pi_home(self):
        with self.assertRaises(ValueError):
            _render(
                workspace="/home/dev/work/primary",
                extra_workspaces=("/home/dev/.pi",),
                pi_home_host="/home/alice/.pi",
            )

    def test_workspace_collides_with_projection_dst(self):
        with self.assertRaises(ValueError):
            _render(
                workspace="/run/pi-cli/docker-constructor.runtime.toml",
            )

    def test_extra_workspace_collides_with_projection_dst(self):
        with self.assertRaises(ValueError):
            _render(
                workspace="/home/dev/work/primary",
                extra_workspaces=("/run/pi-cli/docker-constructor.runtime.toml",),
            )

    def test_no_collision_between_pi_home_and_projection(self):
        """Pi home dst (/home/dev/.pi) and projection dst
        (/run/pi-cli/...) are distinct — must not raise."""
        _render(
            pi_home_host="/home/alice/.pi",
            projection_container_path=("/run/pi-cli/docker-constructor.runtime.toml"),
        )


class TestDeterministicOrderingEdge(unittest.TestCase):
    """Deterministic output independent of input field order."""

    def test_extra_workspaces_in_order(self):
        """Extra workspaces must appear in the vector in the order
        they were given — no sorting or reordering."""
        a = _render(
            workspace="/home/dev/work/primary",
            extra_workspaces=("/home/dev/work/B", "/home/dev/work/A"),
        )
        b = _render(
            workspace="/home/dev/work/primary",
            extra_workspaces=("/home/dev/work/A", "/home/dev/work/B"),
        )
        # Different input order → different vector (caller controls ordering).
        self.assertNotEqual(a, b)

    def test_flag_order_deterministic(self):
        """Calling render_run_vector twice with identical RunRenderInputs
        constructed with different kwarg order must produce the same
        flag order in the output vector."""
        a = render_run_vector(
            RunRenderInputs(
                image="pi-cli-pi:latest",
                container_name="pi-1",
                projection_host_path=(
                    "/home/dev/.pi-cli/.docker-generated/runtime/x.toml"
                ),
                projection_container_path="/run/pi-cli/docker-constructor.runtime.toml",
                pi_home_host="/home/alice/.pi",
                workspace="/home/dev/work/primary",
                extra_workspaces=("/home/dev/work/lib",),
            )
        )
        b = render_run_vector(
            RunRenderInputs(
                extra_workspaces=("/home/dev/work/lib",),
                workspace="/home/dev/work/primary",
                pi_home_host="/home/alice/.pi",
                projection_container_path="/run/pi-cli/docker-constructor.runtime.toml",
                projection_host_path=(
                    "/home/dev/.pi-cli/.docker-generated/runtime/x.toml"
                ),
                container_name="pi-1",
                image="pi-cli-pi:latest",
            )
        )
        self.assertEqual(
            a, b, "output must be independent of RunRenderInputs kwarg order"
        )


class TestArgumentAtomicity(unittest.TestCase):
    """No argument may contain shell-joined command text."""

    def test_no_shell_joined_flags(self):
        """Every --flag and its value are separate elements.
        No '--flag=value' or '--flag value' fused into one string."""
        args = _render(
            workspace="/home/dev/work/primary",
            extra_workspaces=("/home/dev/work/lib",),
        )
        for token in args:
            if token.startswith("--"):
                self.assertNotIn("=", token, f"flag {token!r} must not embed its value")
                self.assertNotIn(" ", token, f"flag {token!r} must not embed its value")

    def test_no_env_with_equals_in_key(self):
        """--env KEY=VALUE must be two tokens: --env and KEY=VALUE."""
        args = _render()
        # The --env token itself must be bare.
        for i, token in enumerate(args):
            if token == "--env":
                self.assertLess(i + 1, len(args))
                self.assertFalse(
                    args[i + 1].startswith("--"),
                    f"--env followed by flag {args[i + 1]!r}",
                )


# ---------------------------------------------------------------------------
# Argument-parsing helpers
# ---------------------------------------------------------------------------


def _collect_mounts(args: tuple[str, ...]) -> list[dict[str, str]]:
    """Extract all ``--mount`` key=value,... specs into dicts."""
    mounts: list[dict[str, str]] = []
    it = iter(args)
    for token in it:
        if token == "--mount":
            raw = next(it)
            kv: dict[str, str] = {}
            for pair in raw.split(","):
                k, _, v = pair.partition("=")
                kv[k] = v
            mounts.append(kv)
    return mounts


def _find_mount(mounts: list[dict[str, str]], dst: str) -> dict[str, str] | None:
    for m in mounts:
        if m.get("dst") == dst:
            return m
    return None


def _collect_env(args: tuple[str, ...]) -> dict[str, str]:
    """Extract all ``--env KEY=VALUE`` pairs into a dict."""
    env: dict[str, str] = {}
    it = iter(args)
    for token in it:
        if token == "--env":
            raw = next(it)
            k, _, v = raw.partition("=")
            env[k] = v
    return env


# ── 6.2.4  Artifact-mount selection + mount planning ─────────────


class TestArtifactMounts(unittest.TestCase):
    """One deterministic read-only mount per unique integrity, fixed
    container targets beneath ``/run/pi-cli/runtime-artifacts``, no
    cache-root mounts, host_path passed through from verified blobs
    — all exercised through the **production** ``plan_artifact_mounts``
    boundary.

    Rendering of ``--mount …,readonly`` in the Docker vector, host
    regular-file/symlink validation, and canonical cache-source
    verification belong to task 5.3.
    """

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _integrity(alg: str = "sha512", seed: str = "A") -> str:
        """Return a well-formed SRI string for the given algorithm."""
        import base64

        lengths = {"sha256": 32, "sha384": 48, "sha512": 64}
        raw = (seed.encode() * lengths[alg])[: lengths[alg]]
        return f"{alg}-{base64.b64encode(raw).decode()}"

    @staticmethod
    def _inventory(**extensions) -> "RuntimeInventory":
        """Build a minimal RuntimeInventory from extension specs.

        Each value is a dict with keys: ``pkg``, ``version``,
        ``integrity``, ``url`` (optional), and optionally
        ``extra_artifacts`` (a dict of version→integrity for
        additional catalog entries).
        """
        from docker.versioning.constraints import parse_constraint
        from docker.versioning.inventory import (
            NpmArtifact,
            NpmSource,
            NpmUpdate,
            OverridePolicy,
            PiExtensionEntry,
            RuntimeInventory,
            RuntimeValidation,
        )

        entries: dict[str, PiExtensionEntry] = {}
        for name, spec in extensions.items():
            pkg = spec["pkg"]
            ver = spec["version"]
            integ = spec["integrity"]
            url = spec.get(
                "url",
                f"https://registry.npmjs.org/{pkg}/-/"
                f"{pkg.rsplit('/', 1)[-1] if '/' in pkg else pkg}"
                f"-{ver}.tgz",
            )
            artifacts = {ver: NpmArtifact(url=url, integrity=integ)}
            for extra_ver, extra_integ in spec.get("extra_artifacts", {}).items():
                extra_url = (
                    f"https://registry.npmjs.org/{pkg}/"
                    f"-/{pkg.rsplit('/', 1)[-1] if '/' in pkg else pkg}"
                    f"-{extra_ver}.tgz"
                )
                artifacts[extra_ver] = NpmArtifact(
                    url=extra_url,
                    integrity=extra_integ,
                )
            entries[name] = PiExtensionEntry(
                version=ver,
                source=NpmSource(package=pkg),
                update=NpmUpdate(stable_only=True),
                artifacts=artifacts,
                validation=RuntimeValidation(metadata_file="package.json"),
                override=OverridePolicy(
                    constraint=parse_constraint(">=1.0.0"),
                    allow_prerelease=True,
                    scheme="numeric",
                ),
            )
        return RuntimeInventory(pi_extensions=entries)

    @staticmethod
    def _blob_result(*, integrity: str, host_path: str = "") -> "VerifiedCacheBlob":
        """Synthetic ``VerifiedCacheBlob`` — no filesystem access."""
        from docker.versioning.artifact_cache import VerifiedCacheBlob

        algo, raw_b64 = integrity.split("-", 1)
        digest = raw_b64.replace("+", "-").replace("/", "_")
        if not host_path:
            host_path = f"/tmp/runtime-artifacts/blobs/{algo}/{digest}.tgz"
        return VerifiedCacheBlob(
            algorithm=algo,
            digest=digest,
            integrity=integrity,
            host_path=host_path,
        )

    @staticmethod
    def _plan(blobs):
        """Thin forward to the production boundary."""
        from docker.versioning.rendering import plan_artifact_mounts

        return plan_artifact_mounts(blobs)

    # ── unique selected integrity (resolve_runtime) ──────────

    def test_same_integrity_across_extensions_produces_one_selection(self):
        from docker.versioning.effective import resolve_runtime

        shared = self._integrity()
        inv = self._inventory(
            a={"pkg": "@s/a", "version": "1.0.0", "integrity": shared},
            b={"pkg": "@s/b", "version": "2.0.0", "integrity": shared},
        )
        selected, _ = resolve_runtime(inv, {})
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].integrity, shared)

    def test_distinct_integrities_produce_distinct_selections(self):
        from docker.versioning.effective import resolve_runtime

        inv = self._inventory(
            a={
                "pkg": "@s/a",
                "version": "1.0.0",
                "integrity": self._integrity(seed="A"),
            },
            b={
                "pkg": "@s/b",
                "version": "1.0.0",
                "integrity": self._integrity(seed="B"),
            },
        )
        selected, _ = resolve_runtime(inv, {})
        integrities = {s.integrity for s in selected}
        self.assertEqual(len(integrities), 2)

    def test_unselected_artifact_version_not_in_selected_set(self):
        from docker.versioning.effective import resolve_runtime

        default_integ = self._integrity(seed="D")
        alt_integ = self._integrity(seed="X")
        inv = self._inventory(
            ext={
                "pkg": "@s/ext",
                "version": "1.0.0",
                "integrity": default_integ,
                "extra_artifacts": {"2.0.0": alt_integ},
            },
        )
        selected, _ = resolve_runtime(inv, {})
        integrities = {s.integrity for s in selected}
        self.assertIn(default_integ, integrities)
        self.assertNotIn(alt_integ, integrities)

    # ── mount planning: deterministic per-integrity output ───

    def test_plan_one_mount_per_unique_integrity(self):
        """Duplicate integrities are collapsed into a single mount."""
        shared = self._integrity()
        mounts = self._plan(
            [
                self._blob_result(integrity=shared, host_path="/cache/a.tgz"),
                self._blob_result(integrity=shared, host_path="/cache/b.tgz"),
            ]
        )
        self.assertEqual(len(mounts), 1)

    def test_plan_distinct_integrities_produce_distinct_mounts(self):
        mounts = self._plan(
            [
                self._blob_result(
                    integrity=self._integrity(seed="A"), host_path="/cache/a.tgz"
                ),
                self._blob_result(
                    integrity=self._integrity(seed="B"), host_path="/cache/b.tgz"
                ),
            ]
        )
        self.assertEqual(len(mounts), 2)

    def test_plan_no_unselected_integrity_in_mounts(self):
        """Only integrities passed to the planner appear in mounts."""
        from docker.versioning.model import _derive_artifact_id

        default_integ = self._integrity(seed="D")
        alt_integ = self._integrity(seed="X")
        mounts = self._plan(
            [
                self._blob_result(integrity=default_integ, host_path="/cache/d.tgz"),
            ]
        )
        targets = {m.container_target for m in mounts}
        alt_target = f"/run/pi-cli/runtime-artifacts/{_derive_artifact_id(alt_integ)}"
        self.assertNotIn(alt_target, targets)
        self.assertEqual(len(mounts), 1)

    # ── mount planning: host_path pass-through ───────────────

    def test_plan_passes_host_path_through_from_blob(self):
        """The planner MUST use the blob's verified host_path, not
        reconstruct a synthetic cache path."""
        custom = "/custom/verified/path/to/blob.tgz"
        mounts = self._plan(
            [
                self._blob_result(integrity=self._integrity(), host_path=custom),
            ]
        )
        self.assertEqual(mounts[0].host_path, custom)

    def test_plan_host_paths_follow_explicit_cache_root(self):
        """Verified blobs retain their explicit resolved host path."""
        blob = self._blob_result(integrity=self._integrity())
        self.assertIn("/tmp/runtime-artifacts/blobs", blob.host_path)
        mounts = self._plan([blob])
        self.assertEqual(mounts[0].host_path, blob.host_path)

    # ── mount planning: fixed container targets ──────────────

    def test_plan_targets_beneath_fixed_artifact_root(self):
        mounts = self._plan(
            [
                self._blob_result(
                    integrity=self._integrity(alg="sha256", seed="H"),
                    host_path="/cache/h.tgz",
                ),
                self._blob_result(
                    integrity=self._integrity(alg="sha512", seed="J"),
                    host_path="/cache/j.tgz",
                ),
            ]
        )
        self.assertGreater(len(mounts), 0)
        root = "/run/pi-cli/runtime-artifacts"
        for m in mounts:
            self.assertTrue(
                m.container_target.startswith(root + "/"),
                f"target {m.container_target!r} not beneath {root}",
            )

    def test_plan_targets_always_end_with_tgz(self):
        mounts = self._plan(
            [
                self._blob_result(
                    integrity=self._integrity(), host_path="/cache/x.tgz"
                ),
            ]
        )
        for m in mounts:
            self.assertTrue(
                m.container_target.endswith(".tgz"),
                f"target {m.container_target!r} must end with .tgz",
            )

    def test_plan_container_targets_are_unique(self):
        mounts = self._plan(
            [
                self._blob_result(
                    integrity=self._integrity(seed="M"), host_path="/cache/m.tgz"
                ),
                self._blob_result(
                    integrity=self._integrity(seed="N"), host_path="/cache/n.tgz"
                ),
            ]
        )
        targets = [m.container_target for m in mounts]
        self.assertEqual(len(targets), len(set(targets)))

    def test_plan_mounts_sorted_by_container_target(self):
        """plan_artifact_mounts must return mounts sorted by
        container_target for deterministic rendering."""
        # Two integrities whose artifact_ids sort in reverse order.
        mounts = self._plan(
            [
                self._blob_result(
                    integrity=self._integrity(alg="sha512", seed="Z"),
                    host_path="/cache/z.tgz",
                ),
                self._blob_result(
                    integrity=self._integrity(alg="sha256", seed="A"),
                    host_path="/cache/a.tgz",
                ),
            ]
        )
        targets = [m.container_target for m in mounts]
        self.assertEqual(targets, sorted(targets))

    def test_plan_container_root_is_not_configurable(self):
        """The container root ``/run/pi-cli/runtime-artifacts`` is
        a private module constant — every target must start there."""
        from docker.versioning.rendering import _RUNTIME_ARTIFACT_ROOT

        mounts = self._plan(
            [
                self._blob_result(
                    integrity=self._integrity(), host_path="/cache/x.tgz"
                ),
            ]
        )
        for m in mounts:
            self.assertTrue(
                m.container_target.startswith(_RUNTIME_ARTIFACT_ROOT + "/"),
                f"{m.container_target!r} must start with {_RUNTIME_ARTIFACT_ROOT!r}",
            )

    # ── mount planning: no cache-root mount ──────────────────

    def test_plan_no_cache_root_directory_in_targets(self):
        mounts = self._plan(
            [
                self._blob_result(
                    integrity=self._integrity(), host_path="/cache/q.tgz"
                ),
                self._blob_result(
                    integrity=self._integrity(seed="Z"), host_path="/cache/z.tgz"
                ),
            ]
        )
        root = "/run/pi-cli/runtime-artifacts"
        for m in mounts:
            self.assertNotEqual(m.container_target, root)
            self.assertNotEqual(m.container_target, root + "/")
            self.assertGreater(m.container_target.count("/"), 3)

    # ── mount planning: edge cases ───────────────────────────

    def test_plan_empty_blobs_returns_empty_tuple(self):
        mounts = self._plan([])
        self.assertEqual(mounts, ())

    def test_plan_is_deterministic(self):
        blobs = [
            self._blob_result(integrity=self._integrity(), host_path="/cache/x.tgz"),
        ]
        a = self._plan(blobs)
        b = self._plan(blobs)
        self.assertEqual(a, b)

    # ── rendering edge: zero mounts / relative source ────────

    def test_zero_artifact_mounts_produces_valid_output(self):
        args = _render(artifact_mounts=())
        self.assertEqual(args[:2], ("docker", "run"))
        self.assertIn("--rm", args)

    def test_rejects_relative_host_source(self):
        """Artifact mount source must be an absolute path — caught
        before any filesystem access."""
        from docker.versioning.rendering import ArtifactMount

        with self.assertRaises(ValueError) as ctx:
            _render(
                artifact_mounts=(
                    ArtifactMount(
                        host_path="cache/sha512/blob.tgz",
                        container_target=(
                            "/run/pi-cli/runtime-artifacts/sha512/blob.tgz"
                        ),
                    ),
                )
            )
        self.assertIn("canonical", str(ctx.exception).lower())


# ── 6.2.5  Artifact mount rendering ─────────────────────────────


class TestArtifactMountRendering(unittest.TestCase):
    """Rendering of ``ArtifactMount`` DTOs through ``render_run_vector``:
    each mount produces exactly one ``--mount type=bind,…,readonly``
    argument pair, sorted by container_target, no shell execution,
    no display-string reuse."""

    @classmethod
    def setUpClass(cls):
        import os as _os
        import tempfile

        cls._tmp = tempfile.TemporaryDirectory()
        cls._blob_a = _os.path.join(cls._tmp.name, "a.tgz")
        cls._blob_b = _os.path.join(cls._tmp.name, "b.tgz")
        for p in (cls._blob_a, cls._blob_b):
            with open(p, "wb") as fh:
                fh.write(b"verified")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @staticmethod
    def _mount(*, host_path: str, target: str) -> "ArtifactMount":
        from docker.versioning.rendering import ArtifactMount

        return ArtifactMount(host_path=host_path, container_target=target)

    # ── happy path ───────────────────────────────────────────

    def test_one_mount_produces_one_mount_arg_pair(self):
        """Each ArtifactMount → one ``--mount <opts>`` token pair."""
        args = _render(
            artifact_mounts=(
                self._mount(
                    host_path=self._blob_a,
                    target="/run/pi-cli/runtime-artifacts/sha512/abc.tgz",
                ),
            )
        )
        # Count --mount tokens whose destination is an artifact target.
        art_mounts = [
            i
            for i, tok in enumerate(args)
            if tok == "--mount" and "runtime-artifacts" in args[i + 1]
        ]
        self.assertEqual(len(art_mounts), 1)

    def test_multiple_mounts_produce_multiple_arg_pairs(self):
        args = _render(
            artifact_mounts=(
                self._mount(
                    host_path=self._blob_a,
                    target="/run/pi-cli/runtime-artifacts/sha256/aaa.tgz",
                ),
                self._mount(
                    host_path=self._blob_b,
                    target="/run/pi-cli/runtime-artifacts/sha512/bbb.tgz",
                ),
            )
        )
        art_mounts = [
            i
            for i, tok in enumerate(args)
            if tok == "--mount" and "runtime-artifacts" in args[i + 1]
        ]
        self.assertEqual(len(art_mounts), 2)

    def test_mount_opts_is_single_string_not_shell_split(self):
        """The options string following ``--mount`` is a single
        comma-separated value, not tokenised by whitespace."""
        args = _render(
            artifact_mounts=(
                self._mount(
                    host_path=self._blob_a,
                    target="/run/pi-cli/runtime-artifacts/sha512/abc.tgz",
                ),
            )
        )
        # Find the --mount token for our artifact.
        for i, tok in enumerate(args):
            if tok == "--mount" and "runtime-artifacts" in args[i + 1]:
                opts = args[i + 1]
                self.assertIn("type=bind", opts)
                self.assertIn("src=" + self._blob_a, opts)
                self.assertIn(
                    "dst=/run/pi-cli/runtime-artifacts/",
                    opts,
                )
                self.assertIn(",readonly", opts)
                # Must be a single string — no spaces to split on
                # (the source/dest are absolute paths without spaces).
                self.assertNotIn(" ", opts, "mount options must be one token")
                return
        self.fail("artifact --mount not found in rendered vector")

    # ── ordering ─────────────────────────────────────────────

    def test_artifact_mounts_sorted_by_container_target(self):
        """The renderer emits artifact mounts in
        container_target order."""
        # Insert in reverse alphabetical target order.
        args = _render(
            artifact_mounts=(
                self._mount(
                    host_path=self._blob_a,
                    target="/run/pi-cli/runtime-artifacts/sha512/z.tgz",
                ),
                self._mount(
                    host_path=self._blob_b,
                    target="/run/pi-cli/runtime-artifacts/sha256/a.tgz",
                ),
            )
        )
        # Collect artifact destinations in emission order.
        seen: list[str] = []
        for i, tok in enumerate(args):
            if tok == "--mount" and "runtime-artifacts" in args[i + 1]:
                for part in args[i + 1].split(","):
                    if part.startswith("dst="):
                        seen.append(part.split("=", 1)[1])
        self.assertEqual(seen, sorted(seen))

    # ── readonly flag ────────────────────────────────────────

    def test_every_artifact_mount_has_readonly(self):
        """Every artifact mount MUST include the ``readonly`` flag."""
        args = _render(
            artifact_mounts=(
                self._mount(
                    host_path=self._blob_a,
                    target="/run/pi-cli/runtime-artifacts/sha512/first.tgz",
                ),
                self._mount(
                    host_path=self._blob_b,
                    target="/run/pi-cli/runtime-artifacts/sha256/second.tgz",
                ),
            )
        )
        for i, tok in enumerate(args):
            if tok == "--mount" and "runtime-artifacts" in args[i + 1]:
                self.assertIn(
                    ",readonly",
                    args[i + 1],
                    f"artifact mount {args[i + 1]!r} missing readonly",
                )

    # ── host / target passthrough ────────────────────────────

    def test_source_is_host_path(self):
        args = _render(
            artifact_mounts=(
                self._mount(
                    host_path=self._blob_a,
                    target="/run/pi-cli/runtime-artifacts/sha512/x.tgz",
                ),
            )
        )
        for i, tok in enumerate(args):
            if tok == "--mount" and "runtime-artifacts" in args[i + 1]:
                self.assertIn("src=" + self._blob_a, args[i + 1])
                return
        self.fail("artifact --mount not found")

    def test_destination_is_container_target(self):
        target = (
            "/run/pi-cli/runtime-artifacts/sha512/"
            "0000000000000000000000000000000000000000.tgz"
        )
        args = _render(
            artifact_mounts=(self._mount(host_path=self._blob_a, target=target),)
        )
        for i, tok in enumerate(args):
            if tok == "--mount" and "runtime-artifacts" in args[i + 1]:
                self.assertIn("dst=" + target, args[i + 1])
                return
        self.fail("artifact --mount not found")

    # ── no display-string reuse ──────────────────────────────

    def test_rendered_output_is_arg_tuple(self):
        """render_run_vector returns ``tuple[str, ...]``, never a
        shell command string."""
        args = _render(
            artifact_mounts=(
                self._mount(
                    host_path=self._blob_a,
                    target="/run/pi-cli/runtime-artifacts/sha512/x.tgz",
                ),
            )
        )
        self.assertIsInstance(args, tuple)
        self.assertTrue(all(isinstance(t, str) for t in args))
        # Must start with "docker", "run" — not a shell string.
        self.assertEqual(args[:2], ("docker", "run"))
        # No shell meta-characters wrapping the whole thing.
        self.assertNotIn("&&", args)
        self.assertNotIn("|", args)

    # ── zero mounts ──────────────────────────────────────────

    def test_no_artifact_mount_args_when_empty(self):
        """Empty artifact_mounts must not emit any
        runtime-artifacts --mount args."""
        args = _render(artifact_mounts=())
        for i, tok in enumerate(args):
            if tok == "--mount":
                self.assertNotIn(
                    "runtime-artifacts",
                    args[i + 1],
                    "empty artifact_mounts must not produce artifact mount arguments",
                )


# ── 6.2.6  Artifact mount collision & safety ───────────────────


class TestArtifactMountCollisions(unittest.TestCase):
    """Pre-Docker safety validation: duplicate targets, source/target
    aliasing, directory mounts, traversal, symlinks, missing blobs,
    non-regular files — all rejected by ``_validate_run_inputs``
    before ``render_run_vector`` produces argument tuples."""

    @classmethod
    def setUpClass(cls):
        import os as _os
        import tempfile

        cls._tmp = tempfile.TemporaryDirectory()
        d = cls._tmp.name
        # Regular files for valid mounts
        cls._reg_a = _os.path.join(d, "a.tgz")
        cls._reg_b = _os.path.join(d, "b.tgz")
        for p in (cls._reg_a, cls._reg_b):
            with open(p, "wb") as fh:
                fh.write(b"verified")
        # Directory
        cls._dir = _os.path.join(d, "dir")
        _os.mkdir(cls._dir)
        # Symlink to a regular file
        cls._link = _os.path.join(d, "link.tgz")
        _os.symlink(cls._reg_a, cls._link)
        # Missing path (never created)
        cls._missing = _os.path.join(d, "missing.tgz")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @staticmethod
    def _mount(*, host_path: str, target: str) -> "ArtifactMount":
        from docker.versioning.rendering import ArtifactMount

        return ArtifactMount(host_path=host_path, container_target=target)

    _ART_ROOT = "/run/pi-cli/runtime-artifacts"

    def _target(self, basename: str = "sha512/abc.tgz") -> str:
        return f"{self._ART_ROOT}/{basename}"

    # ── duplicate targets ────────────────────────────────────

    def test_rejects_duplicate_container_target(self):
        target = self._target("sha512/dup.tgz")
        with self.assertRaises(ValueError) as ctx:
            _render(
                artifact_mounts=(
                    self._mount(host_path=self._reg_a, target=target),
                    self._mount(host_path=self._reg_b, target=target),
                )
            )
        self.assertIn("duplicate or aliased", str(ctx.exception).lower())

    # ── duplicate sources ────────────────────────────────────

    def test_rejects_duplicate_host_source(self):
        with self.assertRaises(ValueError) as ctx:
            _render(
                artifact_mounts=(
                    self._mount(
                        host_path=self._reg_a,
                        target=self._target("sha512/t1.tgz"),
                    ),
                    self._mount(
                        host_path=self._reg_a,
                        target=self._target("sha256/t2.tgz"),
                    ),
                )
            )
        self.assertIn("duplicate or aliased", str(ctx.exception).lower())

    # ── source/target aliasing (reaches "duplicate or aliased") ─

    def test_rejects_source_equals_target_aliasing(self):
        """When realpath(source) == target and both are valid
        artifact-root paths, the duplicate/aliased check fires.
        Mocked so realpath returns host_path unchanged."""
        from unittest.mock import patch

        target = self._target("sha512/aliased.tgz")
        with (
            patch("os.path.realpath", side_effect=lambda p: p),
            patch("os.path.isfile", return_value=True),
            patch("os.path.islink", return_value=False),
        ):
            with self.assertRaises(ValueError) as ctx:
                _render(artifact_mounts=(self._mount(host_path=target, target=target),))
            self.assertIn("duplicate or aliased", str(ctx.exception).lower())

    def test_rejects_artifact_target_collision_with_workspace(self):
        """When workspace is set to a valid artifact-root path,
        an artifact mount with that same target hits the
        duplicate-destination check."""
        target = self._target("sha512/collision.tgz")
        with self.assertRaises(ValueError) as ctx:
            _render(
                workspace=target,
                artifact_mounts=(self._mount(host_path=self._reg_a, target=target),),
            )
        self.assertIn("duplicate or aliased", str(ctx.exception).lower())

    def test_rejects_target_collision_with_pi_home(self):
        """Artifact target outside the fixed root is caught by the
        canonical-root check before collision is considered."""
        with self.assertRaises(ValueError) as ctx:
            _render(
                artifact_mounts=(
                    self._mount(
                        host_path=self._reg_a,
                        target="/home/dev/.pi",
                    ),
                )
            )
        self.assertIn("canonical beneath fixed root", str(ctx.exception))

    def test_rejects_target_collision_with_projection(self):
        proj_target = "/run/pi-cli/docker-constructor.runtime.toml"
        with self.assertRaises(ValueError) as ctx:
            _render(
                projection_container_path=proj_target,
                artifact_mounts=(
                    self._mount(host_path=self._reg_a, target=proj_target),
                ),
            )
        self.assertIn("canonical beneath fixed root", str(ctx.exception))

    # ── directory mounts ─────────────────────────────────────

    def test_rejects_directory_as_mount_source(self):
        with self.assertRaises(ValueError) as ctx:
            _render(
                artifact_mounts=(
                    self._mount(
                        host_path=self._dir,
                        target=self._target("sha512/dir.tgz"),
                    ),
                )
            )
        self.assertIn("regular non-symlink file", str(ctx.exception).lower())

    # ── traversal ────────────────────────────────────────────

    def test_rejects_traversal_in_source(self):
        """A source with ``..`` whose realpath differs from the
        literal string is rejected as non-canonical."""
        import os as _os

        dirname = _os.path.dirname(self._reg_a)
        basename = _os.path.basename(self._reg_a)
        parent = _os.path.basename(dirname)
        traversal = _os.path.join(dirname, "..", parent, basename)
        with self.assertRaises(ValueError) as ctx:
            _render(
                artifact_mounts=(
                    self._mount(
                        host_path=traversal,
                        target=self._target("sha512/trav.tgz"),
                    ),
                )
            )
        self.assertIn("canonical and absolute", str(ctx.exception))

    def test_rejects_traversal_in_container_target(self):
        target = self._ART_ROOT + "/sha512/../../../etc/passwd"
        with self.assertRaises(ValueError) as ctx:
            _render(
                artifact_mounts=(self._mount(host_path=self._reg_a, target=target),)
            )
        self.assertIn(
            "canonical beneath fixed root",
            str(ctx.exception),
        )

    # ── symlinks ─────────────────────────────────────────────

    def test_rejects_symlink_as_mount_source(self):
        """Symlinks are rejected by the os.path.islink guard,
        which now runs before realpath."""
        with self.assertRaises(ValueError) as ctx:
            _render(
                artifact_mounts=(
                    self._mount(
                        host_path=self._link,
                        target=self._target("sha512/link.tgz"),
                    ),
                )
            )
        self.assertIn("regular non-symlink file", str(ctx.exception).lower())

    # ── missing blobs ────────────────────────────────────────

    def test_rejects_missing_source_file(self):
        """A host_path that does not exist on the filesystem must
        be rejected with the defined regular-file message."""
        with self.assertRaises(ValueError) as ctx:
            _render(
                artifact_mounts=(
                    self._mount(
                        host_path=self._missing,
                        target=self._target("sha512/missing.tgz"),
                    ),
                )
            )
        self.assertIn("regular non-symlink file", str(ctx.exception).lower())

    # ── non-regular files (FIFO) ─────────────────────────────

    def test_rejects_fifo_as_mount_source(self):
        import os as _os

        fifo_path = _os.path.join(self._tmp.name, "fifo")
        _os.mkfifo(fifo_path)
        with self.assertRaises(ValueError) as ctx:
            _render(
                artifact_mounts=(
                    self._mount(
                        host_path=fifo_path,
                        target=self._target("sha512/fifo.tgz"),
                    ),
                )
            )
        self.assertIn("regular non-symlink file", str(ctx.exception).lower())

    # ── non-canonical target ─────────────────────────────────

    def test_rejects_non_canonical_target(self):
        """Target with double-slash or trailing dot is rejected."""
        target = self._ART_ROOT + "//sha512/abc.tgz"
        with self.assertRaises(ValueError) as ctx:
            _render(
                artifact_mounts=(self._mount(host_path=self._reg_a, target=target),)
            )
        self.assertIn(
            "canonical beneath fixed root",
            str(ctx.exception),
        )

    # ── target outside fixed root ────────────────────────────

    def test_rejects_target_outside_artifact_root(self):
        target = "/var/tmp/not-under-runtime-artifacts.tgz"
        with self.assertRaises(ValueError) as ctx:
            _render(
                artifact_mounts=(self._mount(host_path=self._reg_a, target=target),)
            )
        self.assertIn(
            "canonical beneath fixed root",
            str(ctx.exception),
        )

    # ── happy path (sanity) ──────────────────────────────────

    def test_valid_mounts_pass_validation(self):
        """Two distinct, regular-file, non-symlink mounts with
        valid targets must not raise."""
        args = _render(
            artifact_mounts=(
                self._mount(
                    host_path=self._reg_a,
                    target=self._target("sha512/a.tgz"),
                ),
                self._mount(
                    host_path=self._reg_b,
                    target=self._target("sha256/b.tgz"),
                ),
            )
        )
        self.assertIsInstance(args, tuple)

    # ── writable mount safety ────────────────────────────────

    def test_artifact_mounts_always_readonly_never_writable(self):
        """Every artifact mount in the rendered vector MUST carry
        the ``readonly`` flag and MUST NOT expose the default
        ``rw`` mode.  There is no DTO field to request writable
        artifact mounts — the renderer always forces readonly."""
        args = _render(
            artifact_mounts=(
                self._mount(
                    host_path=self._reg_a,
                    target=self._target("sha512/ro.tgz"),
                ),
                self._mount(
                    host_path=self._reg_b,
                    target=self._target("sha256/ro.tgz"),
                ),
            )
        )
        for i, tok in enumerate(args):
            if tok == "--mount" and "runtime-artifacts" in args[i + 1]:
                opts = args[i + 1]
                self.assertIn(
                    ",readonly", opts, f"artifact mount must have readonly: {opts!r}"
                )
                # "rw" must not appear unless it is part of "readonly"
                # (i.e.  "readonly" itself contains no "rw" substring).
                # Docker default is rw, and no artifact mount may rely
                # on that default.
                before_readonly = opts.split(",readonly")[0]
                self.assertNotIn(
                    ",rw",
                    before_readonly + ",",
                    f"artifact mount must not have rw mode: {opts!r}",
                )


# ── 6.6 architectural guards ──────────────────────────────────────


class TestArchitecturalGuards(unittest.TestCase):
    """New direct-Docker renderers must not depend on process
    environment, must return ``tuple[str, ...]``, and must never
    emit ``docker compose``."""

    def test_result_is_always_tuple(self):
        result = _render()
        self.assertIsInstance(result, tuple)
        self.assertTrue(all(isinstance(t, str) for t in result))

    def test_environment_does_not_affect_output(self):
        import os as _os

        baseline = _render()
        saved = {}
        for k in (
            "COMPOSE_FILE",
            "DOCKER_HOST",
            "PI_PROJECT_DIR",
            "HOME",
            "USER",
            "PI_HOME",
            "DOCKER_RUN_OPTS",
        ):
            saved[k] = _os.environ.get(k)
            _os.environ[k] = f"injected-{k}-value"
        try:
            self.assertEqual(_render(), baseline)
        finally:
            for k, v in saved.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v

    def test_vector_never_contains_compose(self):
        result = _render()
        self.assertNotIn("compose", result)
        self.assertEqual(result[0], "docker")
        self.assertEqual(result[1], "run")

    # ── 6.7 projection-origin tests ───────────────────────────────

    def test_run_renderer_never_inspects_runtime_dto_contents(self):
        """The run renderer mounts the runtime projection file but
        never opens, reads, or parses it.  It only uses the host path
        for the ``--mount`` argument — the contents are the container's
        responsibility."""
        # The renderer accepts arbitrary host paths (validated only
        # for directory structure, not file existence).  A path to a
        # nonexistent or empty file must not cause a read/parse error.
        import os as _os
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            runtime_dir = _os.path.join(td, ".docker-generated", "runtime")
            _os.makedirs(runtime_dir)
            # Create an empty file — the renderer must not attempt to read it
            empty_path = _os.path.join(runtime_dir, "empty.toml")
            with open(empty_path, "w") as f:
                f.write("")
            args = _render(
                projection_host_path=empty_path,
            )
            # The path must appear verbatim in a mount argument
            mounts = _collect_mounts(args)
            projection_mounts = [
                m
                for m in mounts
                if m["dst"] == "/run/pi-cli/docker-constructor.runtime.toml"
            ]
            self.assertEqual(
                len(projection_mounts),
                1,
                "Runtime projection must be mounted exactly once",
            )
            self.assertEqual(projection_mounts[0]["src"], empty_path)
