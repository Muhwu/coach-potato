#!/usr/bin/env python
"""Coach Potato desktop entry point.

Starts the FastAPI server on a local port, then opens a native window
(pywebview, when available) or the system browser. Used by the PyInstaller
build; also runnable directly: python desktop.py
"""
import socket
import threading
import time
import webbrowser

import uvicorn

from server.app import app

WINDOW_TITLE = "Coach Potato"


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
        webview.create_window(WINDOW_TITLE, url, width=1280, height=880)
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
