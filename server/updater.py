"""In-app updates for the packaged Windows build.

Flow: the frontend notices a newer GitHub release (its existing once-a-day
check) and asks for it to be downloaded in the background; once the file is
verified the banner offers "Restart to update". Applying writes a small
PowerShell helper, starts it detached and exits the app. The helper waits
until the exe is no longer locked, installs, and starts the new version,
showing a small "Updating…" window the whole time so the gap between the app
closing and reopening never looks like a crash.

Two install kinds, decided by what sits next to the running exe:

- **installer**: Inno Setup leaves `unins000.exe` in `{app}`. The new
  `CoachPotatoSetup.exe` runs with `/VERYSILENT` (no wizard, no prompts);
  Inno reuses the previous install folder and tasks.
- **portable**: the plain exe. The helper keeps a `.old` copy, copies the new
  exe over, and puts the old one back if that fails.

Anything else (dev run, macOS, Linux) is `unsupported` and keeps the manual
download link.

Every download is checked against the release's `SHA256SUMS-windows.txt`
(published by CI next to the exes); a release without it is never installed.
The download comes from GitHub over HTTPS through our own client, so Windows
doesn't mark it as from the internet and SmartScreen doesn't prompt.
"""
import hashlib
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import httpx

INSTALLER_ASSET = "CoachPotatoSetup.exe"
PORTABLE_ASSET = "coach-potato-windows.exe"
CHECKSUM_ASSET = "SHA256SUMS-windows.txt"
UNINSTALLER = "unins000.exe"  # Inno Setup's uninstaller, only in an installed copy

# the helper must stay ASCII: paths are passed as arguments, never pasted in
HELPER_SCRIPT = r"""param(
  [int]$AppPid, [string]$Mode, [string]$Source, [string]$Target,
  [string]$Version, [string]$Log)
$ErrorActionPreference = 'Stop'
function Write-Log($m) {
  try { Add-Content -LiteralPath $Log -Value ("{0:s} {1}" -f (Get-Date), $m) } catch {}
}
$form = $null
try {
  Add-Type -AssemblyName System.Windows.Forms
  $form = New-Object System.Windows.Forms.Form
  $form.Text = 'Coach Potato'
  $form.Width = 380; $form.Height = 130
  $form.StartPosition = 'CenterScreen'; $form.TopMost = $true
  $form.FormBorderStyle = 'FixedDialog'; $form.ControlBox = $false
  $label = New-Object System.Windows.Forms.Label
  $label.Text = "Updating Coach Potato to v$Version ...`r`nIt will reopen by itself in a moment."
  $label.Dock = 'Fill'; $label.TextAlign = 'MiddleCenter'
  $form.Controls.Add($label)
  $form.Show()
} catch { $form = $null }
function Invoke-Pump { if ($form) { [System.Windows.Forms.Application]::DoEvents() } }
function Wait-Ms($ms) {
  $end = (Get-Date).AddMilliseconds($ms)
  while ((Get-Date) -lt $end) { Invoke-Pump; Start-Sleep -Milliseconds 100 }
}
try {
  Write-Log "updating to v$Version ($Mode); waiting for pid $AppPid to exit"
  # the exe stays locked until every Coach Potato process is gone (a onefile
  # build runs a bootloader parent too), so wait on the lock, not on a pid
  $deadline = (Get-Date).AddSeconds(90)
  while ($true) {
    try { $fs = [IO.File]::Open($Target, 'Open', 'ReadWrite', 'None'); $fs.Close(); break }
    catch {
      if ((Get-Date) -gt $deadline) { throw "Coach Potato did not close (or $Target is not writable)" }
      Wait-Ms 250
    }
  }
  if ($Mode -eq 'installer') {
    $p = Start-Process -FilePath $Source -PassThru -ArgumentList @(
      '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOCANCEL')
    while (-not $p.HasExited) { Wait-Ms 200 }
    if ($p.ExitCode -ne 0) { throw "installer exited with code $($p.ExitCode)" }
  } else {
    $backup = "$Target.old"
    Copy-Item -LiteralPath $Target -Destination $backup -Force
    try { Copy-Item -LiteralPath $Source -Destination $Target -Force }
    catch { Copy-Item -LiteralPath $backup -Destination $Target -Force; throw }
  }
  Write-Log "installed v$Version"
} catch {
  Write-Log "update failed: $_"
}
# reopen either way: the new version, or the old one if anything went wrong.
# PYINSTALLER_RESET_ENVIRONMENT=1 makes it a fresh instance: otherwise it
# inherits the old app's PyInstaller variables and looks for python312.dll in
# the old app's (deleted) _MEI temp folder.
$env:PYINSTALLER_RESET_ENVIRONMENT = '1'
Start-Process -FilePath $Target -WorkingDirectory (Split-Path -Parent $Target)
Wait-Ms 4000
if ($form) { $form.Close() }
"""


def install_kind(executable=None, frozen=None, platform=None):
    """'installer' | 'portable' | 'unsupported'."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    platform = sys.platform if platform is None else platform
    if not frozen or platform != "win32":
        return "unsupported"
    exe = Path(executable or sys.executable)
    return "installer" if (exe.parent / UNINSTALLER).exists() else "portable"


def asset_name(kind):
    return {"installer": INSTALLER_ASSET, "portable": PORTABLE_ASSET}.get(kind)


def parse_version(tag):
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", tag or "")
    return tuple(int(x) for x in match.groups()) if match else None


def is_newer(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and b and a > b)


def parse_checksums(text):
    """`<sha256>  <filename>` lines (sha256sum format, `*` binary marker ok)."""
    sums = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            sums[parts[1].lstrip("*").strip()] = parts[0].lower()
    return sums


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class UpdateError(Exception):
    pass


class Updater:
    """One per process. `transport` is injectable for tests, `spawn`/`exit_fn`
    stand in for starting the helper and quitting the app."""

    def __init__(self, repo, current_version, updates_dir, kind=None,
                 transport=None, spawn=None, exit_fn=None):
        self.repo = repo
        self.current_version = current_version
        self.updates_dir = Path(updates_dir)
        self.kind = kind if kind is not None else install_kind()
        self._transport = transport
        self._spawn = spawn or _spawn_detached
        self._exit = exit_fn or (lambda: os._exit(0))
        self._lock = threading.Lock()
        self.state = {"status": "idle", "version": None, "downloaded": 0,
                      "total": None, "error": None, "file": None}

    # ---- status ------------------------------------------------------
    def status(self):
        s = {k: v for k, v in self.state.items() if k != "file"}
        s["kind"] = self.kind
        s["supported"] = self.kind in ("installer", "portable")
        s["current_version"] = self.current_version
        s["last_log"] = self.last_log()
        return s

    def last_log(self):
        """Tail of the helper's log from the previous update, so a failed one
        can be explained after the app reopens."""
        try:
            lines = (self.updates_dir / "update.log").read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        return "\n".join(lines[-4:]) or None

    def _set(self, **kw):
        with self._lock:
            self.state.update(kw)

    # ---- download ----------------------------------------------------
    def _client(self):
        return httpx.Client(
            transport=self._transport, follow_redirects=True, timeout=30.0,
            headers={"User-Agent": "coach-potato-updater",
                     "Accept": "application/vnd.github+json"})

    def latest_release(self, client):
        response = client.get(f"https://api.github.com/repos/{self.repo}/releases/latest")
        response.raise_for_status()
        release = response.json()
        assets = {a.get("name"): a.get("browser_download_url")
                  for a in release.get("assets") or []}
        return release.get("tag_name") or "", assets

    def start_download(self):
        """Kick off the background download; no-op while one runs or once the
        same version is ready."""
        if self.kind not in ("installer", "portable"):
            raise UpdateError("in-app updates only work in the Windows app")
        with self._lock:
            if self.state["status"] in ("checking", "downloading", "ready", "installing"):
                return
            self.state.update(status="checking", error=None, downloaded=0, total=None)
        threading.Thread(target=self._download, name="update-download", daemon=True).start()

    def _download(self):
        try:
            self.download()
        except Exception as e:  # network, GitHub, disk: report, never raise
            self._set(status="error", error=str(e) or type(e).__name__)

    def download(self):
        name = asset_name(self.kind)
        with self._client() as client:
            tag, assets = self.latest_release(client)
            version = tag.lstrip("v")
            if not is_newer(tag, self.current_version):
                self._set(status="up_to_date", version=version)
                return
            if name not in assets:
                raise UpdateError(f"release {tag} has no {name}")
            if CHECKSUM_ASSET not in assets:
                raise UpdateError(f"release {tag} has no checksums; download it manually")
            sums_response = client.get(assets[CHECKSUM_ASSET])
            sums_response.raise_for_status()
            expected = parse_checksums(sums_response.text).get(name)
            if not expected:
                raise UpdateError(f"release {tag} has no checksum for {name}")
            self.updates_dir.mkdir(parents=True, exist_ok=True)
            target = self.updates_dir / f"{Path(name).stem}-v{version}.exe"
            self._clean(keep=target)
            if target.exists() and sha256_of(target) == expected:
                self._set(status="ready", version=version, file=str(target))
                return
            self._set(status="downloading", version=version)
            partial = target.with_suffix(".part")
            with client.stream("GET", assets[name]) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0) or None
                self._set(total=total)
                done = 0
                with open(partial, "wb") as f:
                    for chunk in response.iter_bytes(1 << 16):
                        f.write(chunk)
                        done += len(chunk)
                        self._set(downloaded=done)
            if sha256_of(partial) != expected:
                partial.unlink(missing_ok=True)
                raise UpdateError("the download didn't match its checksum; try again later")
            partial.replace(target)
            self._set(status="ready", file=str(target))

    def _clean(self, keep):
        """Old downloads only — the helper's log stays for diagnostics."""
        for path in self.updates_dir.glob("*"):
            if path != keep and path.suffix in (".exe", ".part", ".ps1"):
                try:
                    path.unlink()
                except OSError:
                    pass

    # ---- apply -------------------------------------------------------
    def helper_command(self, executable=None):
        script = self.updates_dir / "apply-update.ps1"
        (self.updates_dir / "update.log").unlink(missing_ok=True)  # one attempt per log
        script.write_text(HELPER_SCRIPT, encoding="ascii")
        return [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-File", str(script),
            "-AppPid", str(os.getpid()), "-Mode", self.kind,
            "-Source", self.state["file"], "-Target", str(executable or sys.executable),
            "-Version", self.state["version"], "-Log", str(self.updates_dir / "update.log"),
        ]

    def apply(self, delay_s=1.5):
        """Start the helper and quit shortly after, so this response still
        reaches the page (which then shows its own 'updating' screen)."""
        with self._lock:
            if self.state["status"] != "ready" or not self.state["file"]:
                raise UpdateError("no update is ready to install")
            self.state["status"] = "installing"
        try:
            self._spawn(self.helper_command())
        except Exception as e:
            self._set(status="ready")
            raise UpdateError(f"couldn't start the updater: {e}") from e
        timer = threading.Timer(delay_s, self._exit)
        timer.daemon = True
        timer.start()
        return {"version": self.state["version"]}


# NOT DETACHED_PROCESS: powershell.exe started without a console exits 0
# straight away and never runs -File (verified on Windows 11 / PS 5.1; that
# is what broke the first real update, v1.81.2 -> v1.81.3). A console-less
# child is enough: it already outlives this process, which os._exit()s.
SPAWN_FLAGS = ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW")


def _spawn_detached(cmd):
    flags = 0
    for name in SPAWN_FLAGS:
        flags |= getattr(subprocess, name, 0)
    # belt and braces with the script's own reset: whatever the helper
    # starts must not inherit this (onefile) app's PyInstaller runtime
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    subprocess.Popen(cmd, creationflags=flags, close_fds=True, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
