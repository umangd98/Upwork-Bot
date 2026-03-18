"""
upwork_client.py — Wraps the python-upwork-oauth2 SDK.

Handles token loading, persistence, and automatic refresh.
"""

import json
import logging
import os

# Allow OAuth2 over plain HTTP (needed for localhost development)
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

import upwork
from upwork.routers import graphql

import config

logger = logging.getLogger(__name__)


def _load_token() -> dict | None:
    """Load saved OAuth2 token from disk, or return None."""
    if not os.path.exists(config.TOKEN_FILE):
        return None
    try:
        with open(config.TOKEN_FILE, "r") as f:
            token = json.load(f)
        if "access_token" in token and "refresh_token" in token:
            return token
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def save_token(token: dict) -> None:
    """Persist an OAuth2 token dict to disk."""
    os.makedirs(os.path.dirname(config.TOKEN_FILE), exist_ok=True)
    with open(config.TOKEN_FILE, "w") as f:
        json.dump(token, f, indent=2)
    logger.info("Token saved to %s", config.TOKEN_FILE)


def has_valid_token() -> bool:
    """Return True if a saved token file exists with the required fields."""
    return _load_token() is not None


def build_config(token: dict | None = None) -> upwork.Config:
    """
    Build an upwork.Config.

    If *token* is provided it will be embedded so the SDK sets up
    automatic refresh.  Otherwise the config is suitable for starting
    the authorization-code flow.
    """
    cfg: dict = {
        "client_id": config.UPWORK_CLIENT_ID,
        "client_secret": config.UPWORK_CLIENT_SECRET,
        "redirect_uri": config.UPWORK_REDIRECT_URI,
    }
    if token:
        cfg["token"] = token
    return upwork.Config(cfg)


def get_client() -> upwork.Client:
    """
    Return an authenticated upwork.Client, ready to make API calls.

    Loads the saved token, builds the SDK client with auto-refresh,
    and patches the token_updater so refreshed tokens are persisted.
    """
    token = _load_token()
    if token is None:
        raise RuntimeError(
            "No saved token found. Run the auth server first "
            "(python entrypoint.py) or use --cli mode."
        )

    cfg = build_config(token)
    client = upwork.Client(cfg)

    # Patch: also persist to disk whenever the SDK refreshes the token
    original_updater = client.refresh_config_from_access_token

    def _persisting_updater(new_token: dict) -> None:
        original_updater(new_token)
        save_token(new_token)
        logger.info("Token auto-refreshed and persisted.")

    # The underlying OAuth2Session stores the updater
    if hasattr(client, "_Client__oauth") and client._Client__oauth is not None:
        client._Client__oauth.token_updater = _persisting_updater

    return client


def execute_graphql(client: upwork.Client, query: str, variables: dict) -> dict:
    """Execute a GraphQL query and return the parsed response."""
    gql = graphql.Api(client)
    result = gql.execute({"query": query, "variables": variables})
    # Persist token in case it was silently refreshed during the call
    try:
        current_token = client.get_actual_config().token
        if current_token:
            save_token(current_token)
    except Exception:
        pass
    return result
