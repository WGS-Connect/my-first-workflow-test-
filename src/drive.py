from __future__ import annotations

import hashlib
import io
import json
import logging
import random
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from .config import DRIVE_ROOT

log = logging.getLogger("audiobook")

SCOPES = [
    "https://www.googleapis.com/auth/drive"
]


class Drive:
    def __init__(self, credentials_json, retry):
        """
        Connect to Google Drive using an OAuth refresh token.

        credentials_json can contain:

        {
            "client_id": "...",
            "client_secret": "...",
            "refresh_token": "..."
        }
        """

        info = json.loads(credentials_json)

        client_id = info.get("client_id")
        client_secret = info.get("client_secret")
        refresh_token = info.get("refresh_token")

        if not client_id:
            raise ValueError("Google Drive OAuth client_id is missing.")

        if not client_secret:
            raise ValueError("Google Drive OAuth client_secret is missing.")

        if not refresh_token:
            raise ValueError("Google Drive OAuth refresh_token is missing.")

        self.retry = retry

        self.credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )

        # Refresh the access token immediately.
        self.credentials.refresh(Request())

        self.svc = build(
            "drive",
            "v3",
            credentials=self.credentials,
            cache_discovery=False,
        )

        self.cache = {}

        self.root = self._folder(DRIVE_ROOT, None)

        log.info(
            "Google Drive OAuth connected. Root: %s",
            DRIVE_ROOT,
        )

    def _folder(self, name, parent):
        key = (name, parent)

        if key in self.cache:
            return self.cache[key]

        safe_name = name.replace("'", "''")

        q = (
            f"name='{safe_name}' "
            "and mimeType='application/vnd.google-apps.folder' "
            "and trashed=false"
        )

        if parent:
            q += f" and '{parent}' in parents"

        r = self.retry(
            lambda: self.svc.files()
            .list(
                q=q,
                spaces="drive",
                fields="files(id,name)",
                pageSize=100,
            )
            .execute(),
            "drive",
        )

        if r.get("files"):
            fid = r["files"][0]["id"]

        else:
            body = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
            }

            if parent:
                body["parents"] = [parent]

            fid = self.retry(
                lambda: self.svc.files()
                .create(
                    body=body,
                    fields="id",
                )
                .execute(),
                "drive",
            )["id"]

            log.info(
                "Created Drive folder: %s",
                name,
            )

        self.cache[key] = fid

        return fid

    def folder_path(self, path):
        parent = self.root

        for part in [x for x in path.split("/") if x]:
            parent = self._folder(part, parent)
