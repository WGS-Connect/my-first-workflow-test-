from __future__ import annotations

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_json(name, default=None):
    path = ROOT / name

    if not path.exists():
        return {} if default is None else default

    text = path.read_text(encoding="utf-8").strip()

    if not text:
        return {} if default is None else default

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON in {name}: {exc}"
        ) from exc


def env(name, required=True):
    value = os.getenv(name, "").strip()

    if required and not value:
        raise RuntimeError(
            f"Missing required secret: {name}. "
            "Check GitHub Actions Secrets and the workflow environment."
        )

    return value


def drive_oauth_json():
    """
    Read Google Drive OAuth credentials from GitHub Actions.

    Supports:
      GOOGLE_DRIVE_CREDENTIALS
      GOOGLE_DRIVE_OAUTH_JSON

    Expected OAuth JSON must contain:
      client_id
      client_secret
      refresh_token
    """

    raw = os.getenv("GOOGLE_DRIVE_CREDENTIALS", "").strip()

    if not raw:
        raw = os.getenv("GOOGLE_DRIVE_OAUTH_JSON", "").strip()

    if not raw:
        raise RuntimeError(
            "Google Drive credentials are empty. Set "
            "GOOGLE_DRIVE_CREDENTIALS or GOOGLE_DRIVE_OAUTH_JSON."
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Google Drive credentials are not valid JSON. "
            "Paste the complete OAuth JSON into the GitHub Secret."
        ) from exc

    # Support downloaded Google OAuth files.
    if isinstance(data, dict):
        if isinstance(data.get("installed"), dict):
            data = data["installed"]

        elif isinstance(data.get("web"), dict):
            data = data["web"]

    if not isinstance(data, dict):
        raise RuntimeError(
            "Google Drive OAuth credentials must be a JSON object."
        )

    required = {
        "client_id",
        "client_secret",
        "refresh_token",
    }

    missing = sorted(required - set(data.keys()))

    if missing:
        if data.get("type") == "service_account":
            raise RuntimeError(
                "A service-account JSON was provided. "
                "This application now requires Google Drive OAuth "
                "user credentials containing client_id, client_secret "
                "and refresh_token."
            )

        raise RuntimeError(
            "Google Drive OAuth JSON is missing: "
            + ", ".join(missing)
        )

    return json.dumps(data)


# ---------------------------------------------------------
# Main configuration files
# ---------------------------------------------------------

CONFIG = load_json("config.json", {})

VOICE = load_json("voice.json", {})


# ---------------------------------------------------------
# Basic configuration
# ---------------------------------------------------------

TIMEZONE = CONFIG.get(
    "timezone",
    "Asia/Karachi"
)

DRIVE_ROOT = "AUDIOBOOK_AUTOMATION"


# ---------------------------------------------------------
# LLM configuration
# ---------------------------------------------------------

LLM = {
    "gemini_primary": "gemini-3.5-flash",

    "gemini_fallbacks": [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ],

    "groq_primary": "openai/gpt-oss-120b",

    "groq_fallbacks": [
        "openai/gpt-oss-20b",
    ],

    "post_script_openrouter":
        "nvidia/nemotron-3-ultra-550b-a55b:free",
}


# ---------------------------------------------------------
# Secrets
# ---------------------------------------------------------

def secret_config():
    return {
        "gemini_api_key": env(
            "GEMINI_API_KEY"
        ),

        "youtube_client_id": env(
            "YOUTUBE_CLIENT_ID"
        ),

        "youtube_client_secret": env(
            "YOUTUBE_CLIENT_SECRET"
        ),

        "youtube_refresh_token": env(
            "YOUTUBE_REFRESH_TOKEN"
        ),

        "google_drive_credentials":
            drive_oauth_json(),

        # Optional Groq key.
        # Required only if your Gemini fallback system
        # actually uses Groq.
        "groq_api_key": os.getenv(
            "GROQ_API_KEY",
            ""
        ).strip(),

        # Optional OpenRouter key.
        # Required only for the post-script OpenRouter step.
        "openrouter_api_key": os.getenv(
            "OPENROUTER_API_KEY",
            ""
        ).strip(),
    }


# ---------------------------------------------------------
# Load secrets once
# ---------------------------------------------------------

SECRETS = secret_config()
