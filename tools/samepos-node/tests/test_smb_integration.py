"""End-to-end tests for the driver's SMB path against a REAL SMB server.

The unit tests in `test_driver.py` stub the transport, which is right for
verifying decisions but proves nothing about the wire. These tests stand up an
actual impacket SMB server on a loopback port and drive the real code path:
NTLM authentication, share enumeration, directory creation, and file transfer
in both directions.

That matters because the two diagnoses the whole install hangs off are
*transport* behaviours, not logic we can invent:

  * a wrong password must surface as STATUS_LOGON_FAILURE  -> bad_credentials
  * an unreachable port must surface as a connect failure  -> unreachable

Getting those two confused sends an operator to re-check a password that was
never wrong, so they are asserted here against a server that really rejects a
bad NTLM response.

What these tests deliberately do NOT cover: the CIM/DCOM and WinRM execution
channels, and everything in `phase0_recon.ps1` that reads Windows state (WMI,
the registry, SQL Server, ODBC). Those need a real Windows host; there is no
faithful way to fake them, and a fake that passes would be worse than no test.
The recon script is separately parse-checked, and verified to degrade cleanly
when every Windows-specific probe fails.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import driver  # noqa: E402

pytest.importorskip("impacket.smbserver", reason="impacket SMB server not available")

SHARE = "TESTSHARE"
USERNAME = "samadmin"
PASSWORD = "s3cret-pw"

# Server code runs in its own interpreter: SimpleSMBServer.start() blocks
# forever, and a thread in the test process would keep the suite from exiting.
_SERVER_SOURCE = """
import logging, sys
from impacket.smbserver import SimpleSMBServer
from impacket import ntlm

root, port, user, password, share = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
logging.basicConfig(level=logging.CRITICAL)
server = SimpleSMBServer(listenAddress="127.0.0.1", listenPort=port)
server.addShare(share, root, "integration test share")
server.setSMB2Support(True)
server.addCredential(user, 0, "", ntlm.compute_nthash(password).hex())
server.start()
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_until_listening(port: int, timeout: float = 20.0) -> bool:
    """Poll until the server accepts. Binding is not instant, and a fixed sleep
    is either flaky or slow; polling is neither."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            try:
                probe.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def _closed_port() -> int:
    """A port with nothing on it: bind, read the number, release it."""
    port = _free_port()
    # Confirm it really refuses, so a failing 'unreachable' test means the
    # driver misclassified rather than something having grabbed the port.
    with socket.socket() as probe:
        probe.settimeout(0.5)
        try:
            probe.connect(("127.0.0.1", port))
        except OSError:
            return port
    pytest.skip("could not find a reliably closed port")
    raise AssertionError("unreachable")


@pytest.fixture(scope="module")
def smb_server(tmp_path_factory: pytest.TempPathFactory):
    """A real SMB server on loopback, serving a temp directory."""
    root = tmp_path_factory.mktemp("smbroot")
    (root / "existing").mkdir()
    (root / "existing" / "seed.txt").write_bytes(b"hello-from-server\n")

    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-c", _SERVER_SOURCE, str(root), str(port), USERNAME, PASSWORD, SHARE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if not _wait_until_listening(port):
            process.terminate()
            _, stderr = process.communicate(timeout=10)
            pytest.skip(f"SMB test server did not start: {stderr.decode('utf-8', 'replace')[:400]}")
        yield {"root": root, "port": port}
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _target(port: int, password: str = PASSWORD) -> driver.Target:
    return driver.Target(host="127.0.0.1", user=USERNAME, password=password, smb_port=port)


# --------------------------------------------------------------------------
# Transfer: the phase-0 loop is put-script, run, get-log
# --------------------------------------------------------------------------


def test_share_enumeration_over_the_wire(smb_server) -> None:
    channel = driver.SmbChannel(_target(smb_server["port"]))
    try:
        shares = channel.list_shares()
    finally:
        channel.close()
    assert SHARE in shares
    # IPC$ authenticates on every host and says nothing about admin rights, so
    # it must never be offered as a transfer target.
    assert "IPC$" not in [
        name for name in shares if name not in driver._UNINTERESTING_SHARES
    ]


def test_put_creates_missing_parent_directories(smb_server) -> None:
    """Payloads go to nested paths that do not exist yet on a fresh box."""
    channel = driver.SmbChannel(_target(smb_server["port"]))
    payload = b"# phase script\nWrite-Host 'hello'\n"
    try:
        written = channel.put_bytes(payload, SHARE, "samepos\\payload\\sync\\phase.ps1")
    finally:
        channel.close()

    landed = smb_server["root"] / "samepos" / "payload" / "sync" / "phase.ps1"
    assert landed.exists(), "file did not reach the server's filesystem"
    assert landed.read_bytes() == payload
    assert written == len(payload)


def test_put_transfers_a_real_script_byte_for_byte(smb_server) -> None:
    source = Path(__file__).resolve().parents[1] / "phase0_recon.ps1"
    original = source.read_bytes()
    channel = driver.SmbChannel(_target(smb_server["port"]))
    try:
        channel.put(str(source), SHARE, "upload\\phase0_recon.ps1")
    finally:
        channel.close()
    landed = smb_server["root"] / "upload" / "phase0_recon.ps1"
    assert landed.read_bytes() == original, "recon script was corrupted in transfer"


def test_get_reads_a_file_back(smb_server) -> None:
    """This is how every phase's log comes back."""
    channel = driver.SmbChannel(_target(smb_server["port"]))
    try:
        data = channel.get_bytes(SHARE, "existing\\seed.txt")
    finally:
        channel.close()
    assert data == b"hello-from-server\n"


def test_put_then_get_round_trips_binary_safely(smb_server) -> None:
    blob = bytes(range(256)) * 64  # every byte value, including NULs and 0x1a
    channel = driver.SmbChannel(_target(smb_server["port"]))
    try:
        channel.put_bytes(blob, SHARE, "roundtrip\\blob.bin")
        fetched = channel.get_bytes(SHARE, "roundtrip\\blob.bin")
    finally:
        channel.close()
    assert fetched == blob


# --------------------------------------------------------------------------
# Diagnosis against real transport behaviour
# --------------------------------------------------------------------------


def test_valid_credentials_authenticate(smb_server) -> None:
    report = driver.diagnose(_target(smb_server["port"]))
    assert report["smb"]["authenticated"] is True
    assert SHARE in report["smb"]["shares"]


def test_wrong_password_is_classified_as_bad_credentials(smb_server) -> None:
    """The real server rejects a bad NTLM response with STATUS_LOGON_FAILURE.

    This is the assertion that keeps an operator from being sent to fix UAC
    token filtering when the password is simply wrong.
    """
    report = driver.diagnose(_target(smb_server["port"], password="definitely-wrong"))
    assert report["smb"]["authenticated"] is False
    assert report["smb"]["diagnosis"] == driver.ACCESS_BAD_CREDENTIALS
    assert "STATUS_LOGON_FAILURE" in report["smb"]["detail"]

    verdict = report["verdict"]
    assert verdict["headless_install_possible"] is False
    assert "wrong username/password" in verdict["reason"]
    assert "not UAC token filtering" in verdict["reason"]


def test_closed_port_is_classified_as_unreachable() -> None:
    report = driver.diagnose(_target(_closed_port()))
    assert report["smb"]["authenticated"] is False
    assert report["smb"]["diagnosis"] == driver.ACCESS_UNREACHABLE
    assert "port-forward" in report["verdict"]["next_step"]


def test_authenticated_without_a_c_dollar_denial_is_not_called_token_filtering(
    smb_server,
) -> None:
    """Regression test for a bug this end-to-end setup exposed.

    The test server authenticates and has no C$ and no execution channel. The
    verdict used to assert "the UAC remote-token filtering signature" purely
    because SMB authenticated -- a diagnosis it had not verified. Prescribing
    the UAC bootstrap here sends the operator to change a policy that was never
    the problem (and it is the shape you get from a non-Windows SMB host, or a
    target where only 445 is forwarded).
    """
    report = driver.diagnose(_target(smb_server["port"]))
    assert report["smb"]["authenticated"] is True
    assert report["smb"]["admin_share_c"] != driver.ACCESS_OK
    assert report["cim"]["usable"] is False
    assert report["winrm"]["usable"] is False

    verdict = report["verdict"]
    assert verdict["headless_install_possible"] is False
    assert "not the" in verdict["reason"] and "token-filtering signature" in verdict["reason"]
    # It must not send them to the UAC bootstrap on this evidence.
    assert "enable-remoting.bat" not in verdict["next_step"]


# --------------------------------------------------------------------------
# The CLI, exercised against the real server
# --------------------------------------------------------------------------


def test_cli_put_and_get_against_the_real_server(
    smb_server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("SAMEPOS_HOST", "127.0.0.1")
    monkeypatch.setenv("SAMEPOS_USER", USERNAME)
    monkeypatch.setenv("SAMEPOS_PASS", PASSWORD)
    monkeypatch.setenv("SAMEPOS_SMB_PORT", str(smb_server["port"]))

    local = tmp_path / "to_upload.ps1"
    local.write_bytes(b"Write-Host 'cli'\n")

    assert driver.main(["put", str(local), "--share", SHARE, "--dest", "cli\\to_upload.ps1"]) == 0
    assert (smb_server["root"] / "cli" / "to_upload.ps1").read_bytes() == b"Write-Host 'cli'\n"

    out_path = tmp_path / "fetched.ps1"
    assert (
        driver.main(
            ["get", "--share", SHARE, "--path", "cli\\to_upload.ps1", "--out", str(out_path)]
        )
        == 0
    )
    assert out_path.read_bytes() == b"Write-Host 'cli'\n"
    capsys.readouterr()  # drain the emitted JSON
