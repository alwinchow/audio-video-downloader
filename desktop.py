"""Desktop wrapper: runs the Flask app in a background thread and shows it in
a native window via pywebview (Windows: Edge WebView2 — the Chromium engine
already built into Windows 11, so nothing extra to install).

This is the entry point PyInstaller packages into the .exe. `server.py` stays
completely unaware of this — it only reacts to `sys.frozen`, set automatically
by PyInstaller, to find its writable data directory and bundled ffmpeg.
"""
import socket
import threading
import webbrowser

import webview

import server as srv

HOST = "127.0.0.1"
PORT = 5000


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, port)) == 0


def run_server():
    # use_reloader=False: the reloader re-execs the process, which doesn't
    # behave inside a frozen .exe and would just launch a second copy.
    srv.app.run(host=HOST, port=PORT, debug=False, use_reloader=False,
                threaded=True)


def main():
    url = f"http://{HOST}:{PORT}/"

    if not port_in_use(PORT):
        threading.Thread(target=run_server, daemon=True).start()
        # Give Flask a moment to bind before pywebview tries to load the page.
        for _ in range(50):
            if port_in_use(PORT):
                break
            threading.Event().wait(0.1)
    # else: already running (e.g. a second launch) — just open a window onto it.

    try:
        webview.create_window("Audio & Video Downloader", url,
                              width=480, height=900, min_size=(380, 600))
        webview.start()
    except Exception:
        # No WebView2 runtime, or pywebview couldn't start for some other
        # reason — fall back to the system browser rather than the app
        # silently doing nothing.
        webbrowser.open(url)
        input("Opened in your browser instead. Press Enter here to quit.\n")


if __name__ == "__main__":
    main()
