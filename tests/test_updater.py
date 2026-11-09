"""In-app updater against a fake GitHub — no network, no processes."""
import hashlib

import httpx
import pytest

from server import updater

REPO = "owner/coach-potato"
NEW_EXE = b"new coach potato build" * 1000


def fake_github(tag="v1.81.0", payload=NEW_EXE, sums=None, assets=None, calls=None):
    digest = hashlib.sha256(payload).hexdigest()
    if sums is None:
        sums = (f"{digest} *CoachPotatoSetup.exe\n"
                f"{digest} *coach-potato-windows.exe\n")
    names = assets if assets is not None else [
        "CoachPotatoSetup.exe", "coach-potato-windows.exe", "SHA256SUMS-windows.txt"]

    def handler(request):
        if calls is not None:
            calls.append(request.url.path)
        path = request.url.path
        if path == f"/repos/{REPO}/releases/latest":
            return httpx.Response(200, json={"tag_name": tag, "assets": [
                {"name": n, "browser_download_url": f"https://dl.example/{n}"}
                for n in names]})
        if path == "/SHA256SUMS-windows.txt":
            return httpx.Response(200, text=sums)
        if path in ("/CoachPotatoSetup.exe", "/coach-potato-windows.exe"):
            return httpx.Response(200, content=payload)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def make(tmp_path, kind="installer", **kw):
    spawned = []
    exited = []
    u = updater.Updater(REPO, "1.80.0", tmp_path / "updates", kind=kind,
                        transport=fake_github(**kw), spawn=spawned.append,
                        exit_fn=lambda: exited.append(True))
    return u, spawned, exited


def test_install_kind(tmp_path):
    exe = tmp_path / "coach-potato.exe"
    exe.write_bytes(b"")
    assert updater.install_kind(exe, frozen=False, platform="win32") == "unsupported"
    assert updater.install_kind(exe, frozen=True, platform="darwin") == "unsupported"
    assert updater.install_kind(exe, frozen=True, platform="win32") == "portable"
    (tmp_path / "unins000.exe").write_bytes(b"")
    assert updater.install_kind(exe, frozen=True, platform="win32") == "installer"


def test_versions_and_checksums():
    assert updater.is_newer("v1.81.0", "1.80.0")
    assert updater.is_newer("1.80.10", "1.80.9")
    assert not updater.is_newer("v1.80.0", "1.80.0")
    assert not updater.is_newer("garbage", "1.80.0")
    sums = updater.parse_checksums("A" * 64 + " *Setup.exe\n" + "b" * 64 + "  x.exe\njunk\n")
    assert sums == {"Setup.exe": "a" * 64, "x.exe": "b" * 64}


@pytest.mark.parametrize("kind,asset", [("installer", "CoachPotatoSetup"),
                                        ("portable", "coach-potato-windows")])
def test_download_verifies_and_is_ready(tmp_path, kind, asset):
    u, _, _ = make(tmp_path, kind=kind)
    u.download()
    s = u.status()
    assert s["status"] == "ready" and s["version"] == "1.81.0" and s["supported"]
    assert s["downloaded"] == len(NEW_EXE)
    path = tmp_path / "updates" / f"{asset}-v1.81.0.exe"
    assert path.read_bytes() == NEW_EXE
    assert not list((tmp_path / "updates").glob("*.part"))


def test_checksum_mismatch_is_refused(tmp_path):
    u, _, _ = make(tmp_path, sums="0" * 64 + " *CoachPotatoSetup.exe\n")
    with pytest.raises(updater.UpdateError, match="checksum"):
        u.download()
    assert not list((tmp_path / "updates").glob("*.exe"))


def test_release_without_checksums_is_never_installed(tmp_path):
    u, _, _ = make(tmp_path, assets=["CoachPotatoSetup.exe"])
    with pytest.raises(updater.UpdateError, match="checksums"):
        u.download()


def test_not_newer_is_up_to_date(tmp_path):
    u, _, _ = make(tmp_path, tag="v1.80.0")
    u.download()
    assert u.status()["status"] == "up_to_date"


def test_already_downloaded_file_is_reused(tmp_path):
    calls = []
    u = updater.Updater(REPO, "1.80.0", tmp_path / "updates", kind="installer",
                        transport=fake_github(calls=calls))
    u.download()
    calls.clear()
    u.state["status"] = "idle"
    u.download()
    assert "/CoachPotatoSetup.exe" not in calls and u.status()["status"] == "ready"


def test_unsupported_install_cannot_download(tmp_path):
    u, _, _ = make(tmp_path, kind="unsupported")
    with pytest.raises(updater.UpdateError):
        u.start_download()


def test_apply_spawns_helper_then_exits(tmp_path):
    u, spawned, exited = make(tmp_path)
    with pytest.raises(updater.UpdateError):
        u.apply()  # nothing downloaded yet
    u.download()
    (tmp_path / "updates" / "update.log").write_text("old attempt")
    assert u.apply(delay_s=0) == {"version": "1.81.0"}
    cmd = spawned[0]
    assert cmd[0] == "powershell.exe"
    args = dict(zip(cmd[8::2], cmd[9::2]))
    assert args["-Mode"] == "installer" and args["-Version"] == "1.81.0"
    assert args["-Source"].endswith("CoachPotatoSetup-v1.81.0.exe")
    script = tmp_path / "updates" / "apply-update.ps1"
    assert cmd[cmd.index("-File") + 1] == str(script)
    assert "/VERYSILENT" in script.read_text()
    assert not (tmp_path / "updates" / "update.log").exists()  # fresh log per attempt
    assert u.status()["status"] == "installing"
    import time
    for _ in range(50):
        if exited:
            break
        time.sleep(0.01)
    assert exited


def test_failed_spawn_keeps_update_ready(tmp_path):
    def boom(cmd):
        raise OSError("no powershell")
    u = updater.Updater(REPO, "1.80.0", tmp_path / "updates", kind="portable",
                        transport=fake_github(), spawn=boom, exit_fn=lambda: None)
    u.download()
    with pytest.raises(updater.UpdateError, match="no powershell"):
        u.apply()
    assert u.status()["status"] == "ready"


def test_last_log_tail(tmp_path):
    u, _, _ = make(tmp_path)
    assert u.last_log() is None
    (tmp_path / "updates").mkdir()
    (tmp_path / "updates" / "update.log").write_text("a\nb\nc\nd\nupdate failed: x\n")
    assert u.last_log() == "b\nc\nd\nupdate failed: x"


def test_helper_is_never_started_detached(monkeypatch):
    # DETACHED_PROCESS makes powershell.exe exit without running the script
    seen = {}
    monkeypatch.setattr(updater.subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(updater.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(updater.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **kw: seen.update(kw))
    updater._spawn_detached(["powershell.exe"])
    assert seen["creationflags"] & 0x8 == 0
    assert seen["creationflags"] & 0x08000000
