"""
auth_server.py — OAuth2 authorization via Flask callback or manual CLI.

Modes
-----
Flask (default):  Run a tiny web server on AUTH_SERVER_PORT.
    /login    → redirects to Upwork authorize URL
    /callback → exchanges code for tokens, saves, shuts down

CLI (--cli flag):  Prints the authorize URL, waits for the user to
    paste the full callback URL, then exchanges the code.
"""

import logging
import os
import signal
import sys
import threading

# Allow OAuth2 over plain HTTP (needed for localhost development)
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

import upwork
from flask import Flask, redirect, request

import config
from upwork_client import build_config, save_token

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_upwork_client: upwork.Client | None = None


def _make_client() -> upwork.Client:
    """Create an unauthenticated Upwork client for the auth-code flow."""
    cfg = build_config()
    return upwork.Client(cfg)


# ---------------------------------------------------------------------------
# Flask mode
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/")
def index():
    return (
        '<h2>Upwork Bot — OAuth2 Setup</h2>'
        '<p><a href="/login">Click here to log in with Upwork</a></p>'
    )


@app.route("/login")
def login():
    global _upwork_client
    _upwork_client = _make_client()
    authorization_url, _state = _upwork_client.get_authorization_url()
    logger.info("Redirecting to Upwork authorization URL")
    return redirect(authorization_url)


@app.route("/callback")
def callback():
    global _upwork_client
    if _upwork_client is None:
        return "Error: session lost. Please visit /login first.", 400

    try:
        full_url = request.url
        # If running behind a proxy or Docker, the scheme may be wrong
        if full_url.startswith("http://") and request.headers.get("X-Forwarded-Proto") == "https":
            full_url = "https://" + full_url[len("http://"):]

        token = _upwork_client.get_access_token(full_url)
        save_token(token)
        logger.info("OAuth2 tokens obtained and saved successfully.")

        # Schedule server shutdown after response is sent
        threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
        return (
            "<h2>✅ Authentication successful!</h2>"
            "<p>Tokens saved. You can close this tab. "
            "The bot will start polling shortly.</p>"
        )
    except Exception as exc:
        logger.exception("Token exchange failed")
        return f"<h2>❌ Authentication failed</h2><pre>{exc}</pre>", 500


def run_flask_auth_server() -> None:
    """Start the Flask auth server (blocks until auth completes)."""
    port = config.AUTH_SERVER_PORT
    print(f"\n🔐  No saved tokens found.")
    print(f"    Open your browser to:  http://localhost:{port}/login\n")
    app.run(host="0.0.0.0", port=port)


# ---------------------------------------------------------------------------
# CLI mode
# ---------------------------------------------------------------------------

def run_cli_auth() -> None:
    """Interactive CLI-based OAuth2 flow."""
    client = _make_client()
    authorization_url, _state = client.get_authorization_url()

    print("\n🔐  Upwork OAuth2 — CLI Mode")
    print("=" * 60)
    print("1. Open the following URL in your browser:\n")
    print(f"   {authorization_url}\n")
    print("2. Authorize the application.")
    print("3. You will be redirected to a URL (it may fail to load).")
    print("   Copy the FULL URL from your browser's address bar")
    print("   and paste it below.\n")

    callback_url = input("Paste callback URL here: ").strip()
    if not callback_url:
        print("❌  No URL provided. Exiting.")
        sys.exit(1)

    try:
        token = client.get_access_token(callback_url)
        save_token(token)
        print("\n✅  Authentication successful! Tokens saved.")
    except Exception as exc:
        print(f"\n❌  Token exchange failed: {exc}")
        sys.exit(1)
