"""Desktop (packaged app) entry point.

The window's storage path matters more than it looks: pywebview defaults to
private_mode=True, which throws localStorage away when the window closes — so
every saved UI preference (column choices, collapsed blocks, saved skill grids,
last-used view per nav section) silently reset on each launch.
"""
import desktop


def test_webview_storage_path_is_a_sibling_of_the_database(tmp_path, monkeypatch):
    monkeypatch.setenv("LOL_DB_PATH", str(tmp_path / "sub" / "lol.sqlite"))
    path = desktop.webview_storage_path()
    assert path == tmp_path / "sub" / "webview"
    assert path.is_dir()  # created up front; pywebview won't make it itself


def test_webview_storage_path_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("LOL_DB_PATH", str(tmp_path / "lol.sqlite"))
    first = desktop.webview_storage_path()
    (first / "marker").write_text("keep me")
    second = desktop.webview_storage_path()
    assert second == first
    assert (second / "marker").read_text() == "keep me"  # never re-created/wiped


def test_webview_storage_moves_with_the_database_location(tmp_path, monkeypatch):
    monkeypatch.setenv("LOL_DB_PATH", str(tmp_path / "a" / "lol.sqlite"))
    first = desktop.webview_storage_path()
    monkeypatch.setenv("LOL_DB_PATH", str(tmp_path / "b" / "lol.sqlite"))
    assert desktop.webview_storage_path() != first
