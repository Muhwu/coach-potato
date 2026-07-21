#!/usr/bin/env python
"""Coach Potato desktop entry point.

Starts the FastAPI server on a local port, then opens a native window
(pywebview, when available) or the system browser. Used by the PyInstaller
build; also runnable directly: python desktop.py
"""
import os
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser

# PyInstaller's --windowed build (no console attached) leaves sys.stdout/
# stderr as None on Windows. Anything that touches them — including
# uvicorn's logging setup, which calls .isatty() on stderr — crashes with
# AttributeError before the window ever opens. Redirect to a null stream
# first, before importing/using anything that might write to them.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import uvicorn

from server.app import app

WINDOW_TITLE = "Coach Potato"


class DesktopApi:
    """Bridge exposed to the page as window.pywebview.api. Lets the frontend
    ask the Python side to open extra native windows — used by the Matchup
    guide's player-comparison, which needs a real, independent second window
    (the main window stays fully interactive) rather than window.open (which
    the WebView2 host turns into a Microsoft Store prompt)."""

    def __init__(self, base_url):
        self.base_url = base_url

    def open_compare(self, my_champion, opp_champion):
        import webview  # available here — main() only reaches this via pywebview
        query = urllib.parse.urlencode({"my": my_champion or "", "opp": opp_champion or ""})
        webview.create_window(
            f"Compare · {my_champion} vs {opp_champion}",
            f"{self.base_url}/compare.html?{query}",
            width=820, height=960)
        return True


def free_port(preferred=8321):
    for candidate in (preferred, 0):
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", candidate))
            port = sock.getsockname()[1]
            sock.close()
            return port
        except OSError:
            sock.close()
    raise RuntimeError("no free port")


def start_server(port):
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def main():
    port = free_port()
    start_server(port)
    url = f"http://127.0.0.1:{port}"
    try:
        import webview  # pywebview: native window when the OS webview exists
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True  # export .md/.csv links
        except (AttributeError, KeyError, TypeError):
            pass  # older pywebview without the settings dict
        webview.create_window(WINDOW_TITLE, url, width=1280, height=880,
                              js_api=DesktopApi(url))
        webview.start()
    except Exception:
        webbrowser.open(url)
        print(f"{WINDOW_TITLE} running at {url}  (Ctrl+C to quit)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
