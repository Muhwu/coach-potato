# Packaging

`.github/workflows/build.yml` is a GitHub Actions workflow that builds
one-file desktop binaries for Linux/Windows/macOS, plus a Windows installer
(see below). Run it from the Actions tab (workflow_dispatch) or push a `v*`
tag; artifacts appear per-OS on the run page. Pushing that workflow file
itself requires a token with the `workflow` scope
(`gh auth refresh -s workflow`) — a plain `contents`-scoped token can't
touch anything under `.github/workflows/`.

Local build (current OS only):

    pip install pyinstaller pywebview
    pyinstaller --onefile --name coach-potato --add-data "static:static" --add-data "VERSION:." desktop.py

On Windows and macOS, add `--windowed` (a.k.a. `--noconsole`) or the app
launches with a console/Terminal window alongside the pywebview window.
`--windowed` is ignored on Linux — PyInstaller has no console concept there.
On macOS, `--windowed` changes the output from a flat `dist/coach-potato`
binary to a proper `dist/coach-potato.app` bundle (the CI workflow zips this
with `ditto` before uploading) — ship the `.app`, not the binary inside it,
or double-clicking will still open Terminal.

## Windows installer

`windows-installer.iss` is an Inno Setup 6 script that wraps the already-built
`dist/coach-potato.exe` in a normal Windows installer — Start Menu shortcut,
optional desktop shortcut, uninstaller entry in "Add/Remove Programs". It
installs per-user under `%LOCALAPPDATA%\Programs\CoachPotato` with
`PrivilegesRequired=lowest`, so no admin rights or UAC prompt are needed;
this matches the app's existing per-user `%APPDATA%\CoachPotato` data
storage (`server/config.py`'s `default_db_path`) — same user, no permission
mismatch between the two locations.

CI compiles it with Inno Setup 6, which ships preinstalled on GitHub's
`windows-latest` runner image at
`%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe` — no separate install step
needed. The version shown in the installer UI comes from the repo's
`VERSION` file, passed in as an ISCC `/D` define
(`/DMyAppVersion=<contents of VERSION>`); the output lands at
`dist/installer/CoachPotatoSetup.exe` and is uploaded as its own artifact
(`coach-potato-windows-installer`), alongside — not instead of — the plain
portable `.exe`.

Local build (Windows, with Inno Setup 6 installed from
[jrsoftware.org](https://jrsoftware.org/isinfo.php)):

    iscc /DMyAppVersion=0.0.0-dev packaging\windows-installer.iss
