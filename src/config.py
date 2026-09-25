from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_json(name):
    with open(ROOT / name, encoding="utf-8") as f:
        return json.load(f)


def env(name, required=True):
    value = os.getenv(name, "").strip()
    if required and not value:
        raise RuntimeError(
            f"Missing required secret: {name}. "
            "Check that the GitHub Actions workflow passes this secret "
            "and that it is available to the selected environment."
        )
    return value


def drive_oauth_json():
    """Return Drive OAuth credentials in the flat shape expected by Drive."""
    raw = os.getenv("GOOGLE_DRIVE_CREDENTIALS", "").strip()

    # Permit the older/alternate secret name while migrating secrets.
    if not raw:
        raw = os.getenv("GOOGLE_DRIVE_OAUTH_JSON", "").strip()

    if not raw:
        raise RuntimeError(
            "Google Drive credentials are empty. Set "
            "GOOGLE_DRIVE_CREDENTIALS (or GOOGLE_DRIVE_OAUTH_JSON) "
            "to complete OAuth JSON."
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Google Drive credentials are not valid JSON. "
            "Paste the complete JSON object into the GitHub secret."
        ) from exc

    # Google OAuth downloads can be wrapped in {"installed": {...}} or
    # {"web": {...}}. Drive expects the inner object.
    if isinstance(data, dict):
        if isinstance(data.get("installed"), dict):
            data = data["installed"]
        elif isinstance(data.get("web"), dict):
            data = data["web"]

    required = {"client_id", "client_secret", "refresh_token"}
    missing = sorted(required - set(data) if isinstance(data, dict) else required)
    if missing:
        if isinstance(data, dict) and data.get("type") == "service_account":
            raise RuntimeError(
                "GOOGLE_DRIVE_CREDENTIALS contains service-account JSON, "
                "but this application currently requires OAuth JSON with "
                "client_id, client_secret, and refresh_token."
            )
        raise RuntimeError(
            "Google Drive OAuth JSON is missing: " + ", ".join(missing)
        )

    return json.dumps(data)


CONFIG = load_json("config.json")
VOICE = load_json("voice.json")
TIMEZONE = CONFIG["timezone"]
DRIVE_ROOT = "AUDIOBOOK_AUTOMATION"


def secret_config():
    return {
        "gemini_api_key": env("GEMINI_API_KEY"),
        "youtube_client_id": env("YOUTUBE_CLIENT_ID"),
        "youtube_client_secret": env("YOUTUBE_CLIENT_SECRET"),
        "youtube_refresh_token": env("YOUTUBE_REFRESH_TOKEN"),
        "google_drive_credentials": drive_oauth_json(),
    }
