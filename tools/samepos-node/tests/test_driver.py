"""Tests for the SAMePOS install driver.

These cover the logic that must be right *before* the driver is pointed at a
live trading venue: how transport errors are classified (the wrong call here
sends you chasing a password that was never wrong), how commands are built and
quoted for the target, and how the access verdict is reached. Transport itself
is stubbed -- there is no Windows box in CI, and the point is to verify the
decisions, not impacket.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import driver  # noqa: E402


# --------------------------------------------------------------------------
# Error classification -- the runbook's central diagnostic distinction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # A wrong password reports LOGON_FAILURE ...
        ("SMB SessionError: STATUS_LOGON_FAILURE(The attempted logon is invalid)", driver.ACCESS_BAD_CREDENTIALS),
        ("STATUS_ACCOUNT_LOCKED_OUT", driver.ACCESS_BAD_CREDENTIALS),
        ("STATUS_PASSWORD_EXPIRED", driver.ACCESS_BAD_CREDENTIALS),
        # ... while correct-but-token-filtered credentials report ACCESS_DENIED.
        ("SMB SessionError: STATUS_ACCESS_DENIED(Access is denied)", driver.ACCESS_TOKEN_FILTERED),
        ("rpc_s_access_denied", driver.ACCESS_TOKEN_FILTERED),
        ("DCOM error 0x80070005", driver.ACCESS_TOKEN_FILTERED),
        ("[Errno 111] Connection refused", driver.ACCESS_UNREACHABLE),
        ("timed out", driver.ACCESS_UNREACHABLE),
        ("No route to host", driver.ACCESS_UNREACHABLE),
        ("something entirely novel", driver.ACCESS_ERROR),
    ],
)
def test_classify_error(message: str, expected: str) -> None:
    assert driver.classify_error(Exception(message)) == expected


def test_bad_credentials_and_denial_are_never_conflated() -> None:
    """The two must not collapse: they lead to opposite next actions."""
    bad = driver.classify_error(Exception("STATUS_LOGON_FAILURE"))
    denied = driver.classify_error(Exception("STATUS_ACCESS_DENIED"))
    assert bad != denied


# --------------------------------------------------------------------------
# Target parsing
# --------------------------------------------------------------------------


def test_target_from_env_parses_downlevel_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMEPOS_HOST", "203.0.113.10")
    monkeypatch.setenv("SAMEPOS_USER", "VENUEPC\\samadmin")
    monkeypatch.setenv("SAMEPOS_PASS", "secret")
    target = driver.Target.from_env()
    assert (target.host, target.domain, target.user) == ("203.0.113.10", "VENUEPC", "samadmin")
    assert target.winrm_endpoint == "http://203.0.113.10:5985/wsman"


def test_target_from_env_parses_upn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMEPOS_HOST", "host")
    monkeypatch.setenv("SAMEPOS_USER", "samadmin@venue.local")
    monkeypatch.setenv("SAMEPOS_PASS", "secret")
    target = driver.Target.from_env()
    assert (target.user, target.domain) == ("samadmin", "venue.local")


def test_target_from_env_bare_account_has_no_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMEPOS_HOST", "host")
    monkeypatch.setenv("SAMEPOS_USER", "Administrator")
    monkeypatch.setenv("SAMEPOS_PASS", "secret")
    assert driver.Target.from_env().domain == ""


def test_target_from_env_reports_every_missing_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SAMEPOS_HOST", "SAMEPOS_USER", "SAMEPOS_PASS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as excinfo:
        driver.Target.from_env()
    message = str(excinfo.value)
    assert "SAMEPOS_HOST" in message and "SAMEPOS_USER" in message and "SAMEPOS_PASS" in message


def test_password_is_never_taken_from_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """A password on the command line would leak into history and process lists."""
    monkeypatch.setenv("SAMEPOS_HOST", "host")
    monkeypatch.setenv("SAMEPOS_USER", "u")
    monkeypatch.delenv("SAMEPOS_PASS", raising=False)
    parser_args = driver.argparse.Namespace(host="host", user="u", password="hunter2")
    with pytest.raises(SystemExit):
        driver.Target.from_env(parser_args)


# --------------------------------------------------------------------------
# Command construction
# --------------------------------------------------------------------------


def test_build_logged_command_creates_the_log_directory_first() -> None:
    """cmd redirection fails outright if the log's parent is missing, which
    otherwise presents as a phase that did nothing and left no log."""
    built = driver.build_logged_command("do-thing.exe", "E:\\samepos_logs\\phase4.log")
    assert 'if not exist "E:\\samepos_logs" mkdir "E:\\samepos_logs"' in built
    assert built.startswith("cmd.exe /c ")
    assert '> "E:\\samepos_logs\\phase4.log" 2>&1' in built


def test_build_logged_command_captures_both_streams() -> None:
    built = driver.build_logged_command("x", "C:\\logs\\a.log")
    assert "2>&1" in built


def test_build_logged_command_normalises_forward_slashes_in_log_dir() -> None:
    built = driver.build_logged_command("x", "E:/logs/a.log")
    assert 'if not exist "E:\\logs"' in built


def test_build_powershell_command_bypasses_profile_and_policy() -> None:
    built = driver.build_powershell_command("C:\\SamePOS-Server\\configure-node.ps1")
    assert "-NoProfile" in built
    assert "-ExecutionPolicy Bypass" in built
    assert '-File "C:\\SamePOS-Server\\configure-node.ps1"' in built


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("plain", "plain"), ("it's", "it''s"), ("''", "''''"), ("", "")],
)
def test_ps_single_quote(raw: str, expected: str) -> None:
    assert driver._ps_single_quote(raw) == expected


# --------------------------------------------------------------------------
# SMB helpers
# --------------------------------------------------------------------------


class _FakeSmb:
    """Records createDirectory calls; raises on a duplicate, like a real server."""

    def __init__(self) -> None:
        self.created: list[str] = []

    def createDirectory(self, share: str, path: str) -> None:  # noqa: N802 - impacket's name
        if path in self.created:
            raise Exception("STATUS_OBJECT_NAME_COLLISION")
        self.created.append(path)


def test_ensure_dirs_walks_the_path_and_omits_the_filename() -> None:
    fake = _FakeSmb()
    driver.SmbChannel._ensure_dirs(fake, "E", "samepos\\payload\\sync\\file.py")
    assert fake.created == ["samepos", "samepos\\payload", "samepos\\payload\\sync"]


def test_ensure_dirs_tolerates_existing_directories() -> None:
    fake = _FakeSmb()
    fake.created.append("samepos")
    driver.SmbChannel._ensure_dirs(fake, "E", "samepos\\a\\f.txt")
    assert "samepos\\a" in fake.created


def test_ensure_dirs_handles_a_bare_filename() -> None:
    fake = _FakeSmb()
    driver.SmbChannel._ensure_dirs(fake, "E", "file.txt")
    assert fake.created == []


# --------------------------------------------------------------------------
# WinRM loopback launch
# --------------------------------------------------------------------------


class _FakeWinRmSession:
    def __init__(self, stdout: str = "4321\n", stderr: str = "", code: int = 0) -> None:
        self.scripts: list[str] = []
        self._stdout, self._stderr, self._code = stdout, stderr, code

    def run_ps(self, script: str):  # noqa: ANN201 - mimics pywinrm's Response
        self.scripts.append(script)

        class _Response:
            status_code = self._code
            std_out = self._stdout.encode()
            std_err = self._stderr.encode()

        return _Response()


def _winrm_with(session: _FakeWinRmSession) -> driver.WinRmChannel:
    target = driver.Target(host="h", user="u", password="p")
    channel = driver.WinRmChannel(target)
    channel._session = session
    return channel


def test_winrm_launch_detaches_via_cim_and_returns_the_pid() -> None:
    session = _FakeWinRmSession(stdout="4321\n")
    channel = _winrm_with(session)
    pid = channel.launch('cmd.exe /c "thing"')
    assert pid == 4321
    script = session.scripts[0]
    # Detaching matters: a WinRM-child process dies with the WinRM operation,
    # which would kill a 2-minute configure run partway through.
    assert "Win32_Process" in script and "Create" in script


def test_winrm_launch_escapes_embedded_quotes() -> None:
    session = _FakeWinRmSession()
    channel = _winrm_with(session)
    channel.launch("echo it's fine")
    assert "it''s fine" in session.scripts[0]


def test_winrm_launch_raises_on_nonzero_status() -> None:
    channel = _winrm_with(_FakeWinRmSession(stdout="", stderr="denied", code=1))
    with pytest.raises(RuntimeError, match="denied"):
        channel.launch("x")


def test_winrm_poll_maps_states() -> None:
    assert _winrm_with(_FakeWinRmSession(stdout="GONE\n")).poll(1) == driver.PROCESS_GONE
    assert _winrm_with(_FakeWinRmSession(stdout="RUNNING\n")).poll(1) == driver.PROCESS_RUNNING


# --------------------------------------------------------------------------
# Verdict logic -- the install/no-install fork
# --------------------------------------------------------------------------


def _report(**overrides):
    base = {"smb": {}, "cim": {"usable": False}, "winrm": {"usable": False}}
    base.update(overrides)
    return base


def test_verdict_bad_password_is_not_reported_as_token_filtering() -> None:
    verdict = driver._verdict(
        _report(smb={"authenticated": False, "diagnosis": driver.ACCESS_BAD_CREDENTIALS})
    )
    assert verdict["headless_install_possible"] is False
    assert "wrong username/password" in verdict["reason"]
    assert "token filtering" in verdict["reason"]  # explicitly ruled out


def test_verdict_unreachable_points_at_the_port_forward() -> None:
    verdict = driver._verdict(
        _report(smb={"authenticated": False, "diagnosis": driver.ACCESS_UNREACHABLE})
    )
    assert "port-forward" in verdict["next_step"]


def test_verdict_token_filtering_prescribes_the_interactive_bootstrap() -> None:
    verdict = driver._verdict(
        _report(smb={"authenticated": True, "admin_share_c": driver.ACCESS_TOKEN_FILTERED})
    )
    assert verdict["headless_install_possible"] is False
    assert "LocalAccountTokenFilterPolicy" in verdict["next_step"]
    assert "enable-remoting.bat" in verdict["next_step"]


def test_verdict_cim_available_means_go() -> None:
    verdict = driver._verdict(_report(smb={"authenticated": True}, cim={"usable": True}))
    assert verdict["headless_install_possible"] is True
    assert "CIM" in verdict["reason"]


def test_verdict_winrm_only_still_means_go() -> None:
    verdict = driver._verdict(
        _report(smb={"authenticated": True}, cim={"usable": False}, winrm={"usable": True})
    )
    assert verdict["headless_install_possible"] is True
    assert "WinRM" in verdict["reason"]


def test_try_json_survives_non_json_output() -> None:
    assert driver._try_json('{"a": 1}') == {"a": 1}
    assert driver._try_json("not json at all") == "not json at all"
    assert driver._try_json("") == {}


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


def _parse(argv: list[str]):
    import contextlib
    import io as _io

    with contextlib.redirect_stderr(_io.StringIO()):
        return driver.build_parser().parse_args(argv)


def test_cli_subcommand_dest_does_not_collide_with_launch_command_line() -> None:
    """`--command` would have shadowed the subparser dest and broken dispatch."""
    parsed = _parse(["launch", "--command-line", "x.exe", "--log", "C:\\l.log"])
    assert parsed.cmd == "launch"
    assert parsed.command_line == "x.exe"


def test_cli_launch_requires_exactly_one_of_script_or_command() -> None:
    with pytest.raises(SystemExit):
        _parse(["launch", "--log", "C:\\l.log"])
    with pytest.raises(SystemExit):
        _parse(["launch", "--script", "a.ps1", "--command-line", "b.exe", "--log", "C:\\l.log"])


def test_cli_defaults_to_the_cim_channel() -> None:
    """CIM is the channel that works before WinRM has been enabled."""
    assert _parse(["probe"]).channel == "cim"


def test_cli_exposes_every_documented_subcommand() -> None:
    for argv in (
        ["probe"],
        ["shares"],
        ["put", "local.ps1", "--share", "E", "--dest", "a\\b.ps1"],
        ["get", "--share", "E", "--path", "a.log"],
        ["exec", "Get-Date"],
        ["poll", "1234"],
    ):
        assert _parse(argv).cmd == argv[0]


def test_main_probe_emits_the_diagnosis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMEPOS_HOST", "h")
    monkeypatch.setenv("SAMEPOS_USER", "u")
    monkeypatch.setenv("SAMEPOS_PASS", "p")
    monkeypatch.setattr(driver, "diagnose", lambda target: {"stub": True})

    printed: list[str] = []
    monkeypatch.setattr(driver, "_emit", lambda payload: printed.append(json.dumps(payload)))

    assert driver.main(["probe"]) == 0
    assert json.loads(printed[0]) == {"stub": True}


def test_main_launch_builds_a_detached_logged_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMEPOS_HOST", "h")
    monkeypatch.setenv("SAMEPOS_USER", "u")
    monkeypatch.setenv("SAMEPOS_PASS", "p")

    launched: dict[str, object] = {}

    class _StubChannel:
        def launch(self, command: str, cwd: str | None = None) -> int:
            launched["command"] = command
            launched["cwd"] = cwd
            return 999

        def close(self) -> None:
            launched["closed"] = True

    monkeypatch.setattr(driver, "make_exec_channel", lambda target, channel: _StubChannel())
    emitted: list[dict] = []
    monkeypatch.setattr(driver, "_emit", lambda payload: emitted.append(payload))

    rc = driver.main(
        [
            "launch",
            "--script",
            "E:\\samepos\\phase0_recon.ps1",
            "--log",
            "E:\\samepos_logs\\phase0.log",
        ]
    )

    assert rc == 0
    assert emitted[0]["pid"] == 999
    command = launched["command"]
    assert "phase0_recon.ps1" in command
    assert "-ExecutionPolicy Bypass" in command
    assert 'mkdir "E:\\samepos_logs"' in command
    assert launched["closed"] is True


def test_make_exec_channel_rejects_an_unknown_channel() -> None:
    target = driver.Target(host="h", user="u", password="p")
    with pytest.raises(SystemExit, match="unknown channel"):
        driver.make_exec_channel(target, "telepathy")
