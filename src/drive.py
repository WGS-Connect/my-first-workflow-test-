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

        return parent

    def _file(self, name, parent):
        safe_name = name.replace("'", "''")

        q = (
            f"name='{safe_name}' "
            f"and '{parent}' in parents "
            "and trashed=false"
        )

        r = self.retry(
            lambda: self.svc.files()
            .list(
                q=q,
                spaces="drive",
                fields="files(id,name,mimeType,size,md5Checksum)",
                pageSize=100,
            )
            .execute(),
            "drive",
        )

        return r["files"][0] if r.get("files") else None

    def list_folder(self, remote_path):
        parent = self.folder_path(remote_path)

        q = f"'{parent}' in parents and trashed=false"

        files = []
        token = None

        while True:
            r = self.retry(
                lambda: self.svc.files()
                .list(
                    q=q,
                    spaces="drive",
                    fields=(
                        "nextPageToken,"
                        "files(id,name,mimeType,size,md5Checksum)"
                    ),
                    pageSize=1000,
                    orderBy="name",
                    pageToken=token,
                )
                .execute(),
                "drive",
            )

            files.extend(r.get("files", []))

            token = r.get("nextPageToken")

            if not token:
                break

        return files

    def put_file(
        self,
        local,
        remote,
        mimetype="application/octet-stream",
    ):
        """
        Streaming/resumable upload.

        The file is never loaded completely into RAM.
        """

        local = Path(local)

        if not local.exists():
            raise FileNotFoundError(str(local))

        parts = remote.split("/")

        name = parts[-1]

        parent = self.folder_path(
            "/".join(parts[:-1])
        )

        existing = self._file(name, parent)

        def upload(request_factory):
            for attempt in range(1, self.retry.n + 1):

                try:
                    req = request_factory()

                    response = None

                    while response is None:
                        _, response = req.next_chunk()

                    return response

                except Exception as exc:

                    # Check whether the upload actually succeeded
                    # even though the final response was lost.
                    try:
                        found = self._file(name, parent)

                        if found and found.get("size"):

                            if int(found["size"]) == local.stat().st_size:

                                if (
                                    found.get("md5Checksum")
                                    == self._md5(local)
                                ):
                                    return found

                    except Exception:
                        pass

                    if attempt == self.retry.n:
                        raise

                    delay = (
                        self.retry.lo
                        + (
                            self.retry.hi
                            - self.retry.lo
                        )
                        * random.random()
                    )

                    log.warning(
                        "Drive upload failed: %s; "
                        "retrying in %.1fs",
                        exc,
                        delay,
                    )

                    time.sleep(delay)

            raise RuntimeError(
                "Drive upload failed"
            )

        if existing:

            return upload(
                lambda: self.svc.files()
                .update(
                    fileId=existing["id"],
                    media_body=MediaFileUpload(
                        str(local),
                        mimetype=mimetype,
                        chunksize=8 * 1024 * 1024,
                        resumable=True,
                    ),
                    fields="id,name,size,md5Checksum",
                )
            )

        return upload(
            lambda: self.svc.files()
            .create(
                body={
                    "name": name,
                    "parents": [parent],
                },
                media_body=MediaFileUpload(
                    str(local),
                    mimetype=mimetype,
                    chunksize=8 * 1024 * 1024,
                    resumable=True,
                ),
                fields="id,name,size,md5Checksum",
            )
        )

    @staticmethod
    def _md5(path):
        h = hashlib.md5()

        with Path(path).open("rb") as f:
            for chunk in iter(
                lambda: f.read(8 * 1024 * 1024),
                b"",
            ):
                h.update(chunk)

        return h.hexdigest()

    def put_bytes(self, local, remote):
        return self.put_file(
            local,
            remote,
        )

    def download_file(self, file_id, local):
        out = Path(local)

        out.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        part = out.with_suffix(
            out.suffix + ".part"
        )

        def dl():
            req = self.svc.files().get_media(
                fileId=file_id
            )

            with part.open("wb") as f:

                downloader = MediaIoBaseDownload(
                    f,
                    req,
                    chunksize=8 * 1024 * 1024,
                )

                done = False

                while not done:
                    _, done = downloader.next_chunk()

            part.replace(out)

            return out

        self.retry(
            dl,
            "drive",
        )

        return out

    def download_file_by_remote(
        self,
        remote,
        local,
    ):
        parts = remote.split("/")

        parent = self.folder_path(
            "/".join(parts[:-1])
        )

        item = self._file(
            parts[-1],
            parent,
        )

        if not item:
            raise FileNotFoundError(remote)

        return self.download_file(
            item["id"],
            local,
        )

    def get_bytes(self, remote):
        parts = remote.split("/")

        parent = self.folder_path(
            "/".join(parts[:-1])
        )

        item = self._file(
            parts[-1],
            parent,
        )

        if not item:
            raise FileNotFoundError(remote)

        out = io.BytesIO()

        def dl():
            req = self.svc.files().get_media(
                fileId=item["id"]
            )

            downloader = MediaIoBaseDownload(
                out,
                req,
                chunksize=8 * 1024 * 1024,
            )

            done = False

            while not done:
                _, done = downloader.next_chunk()

            return out.getvalue()

        return self.retry(
            dl,
            "drive",
        )

    def delete(self, remote):
        parts = remote.split("/")

        parent = self.folder_path(
            "/".join(parts[:-1])
        )

        item = self._file(
            parts[-1],
            parent,
        )

        if item:
            self.retry(
                lambda: self.svc.files()
                .delete(
                    fileId=item["id"]
                )
                .execute(),
                "drive",
            )

    def delete_tree(self, remote_path):
        items = self.list_folder(
            remote_path
        )

        for item in items:

            child = (
                f"{remote_path.rstrip('/')}/"
                f"{item['name']}"
            )

            if (
                item.get("mimeType")
                == "application/vnd.google-apps.folder"
            ):

                self.delete_tree(child)

                self._delete_id(
                    item["id"]
                )

            else:
                self._delete_id(
                    item["id"]
                )

    def _delete_id(self, file_id):
        self.retry(
            lambda: self.svc.files()
            .delete(
                fileId=file_id
            )
            .execute(),
            "drive",
        )
