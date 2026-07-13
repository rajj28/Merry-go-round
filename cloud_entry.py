"""Cloud entrypoint for Loop (Hugging Face Spaces / any Docker host).

Runs a tiny HTTP health server on $PORT (Spaces expects the container to
answer on 7860) in a daemon thread, then boots the normal Socket Mode app.
The health page doubles as the keep-alive target for uptime pingers.
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib naming
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Loop is running. Slack shows you messages; Loop shows you your obligations.")

    def log_message(self, *args):  # keep container logs clean
        return


def _serve_health() -> None:
    port = int(os.environ.get("PORT", "7860"))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()


def _keep_alive() -> None:
    """Ping our own public URL so free-tier hosts (Render) never idle out.

    Only inbound traffic through the host's proxy resets the idle timer, so the
    request must go to the public URL, not localhost. No-op when the host does
    not provide one (e.g. local runs).
    """
    import time
    import urllib.request

    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url:
        return
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(url, timeout=30).read(64)
        except Exception:
            pass  # transient failures are fine; the next ping retries


def main() -> None:
    threading.Thread(target=_serve_health, daemon=True).start()
    threading.Thread(target=_keep_alive, daemon=True).start()
    from loop.slack_app import main as slack_main

    slack_main()


if __name__ == "__main__":
    main()
