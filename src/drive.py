from __future__ import annotations

import hashlib
import io
import json
import logging
import random
import time
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import (
    MediaFileUpload,
    MediaIoBaseDownload,
)

from .config import CONFIG


log = logging.getLogger("audiobook.drive")


SCOPES = [
    "https://www.googleapis.com/auth/drive"
]

FOLDER_MIME = (
    "application/vnd.google-apps.folder"
)


# ============================================================
# DRIVE ROOT
# ============================================================

DRIVE_ROOT = CONFIG.get(
    "drive_root",
    "AUDIOBOOK"
)


# ============================================================
# DRIVE CLIENT
# ============================================================

class Drive:

    def __init__(
        self,
        credentials_json,
        retry
    ):

        if not credentials_json:
            raise RuntimeError(
                "Google Drive credentials are missing."
            )

        try:
            info = json.loads(
                credentials_json
            )
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "GOOGLE_DRIVE_CREDENTIALS does not contain "
                "valid service-account JSON."
            ) from exc

        self.credentials = (
            Credentials.from_service_account_info(
                info,
                scopes=SCOPES
            )
        )

        self.svc = build(
            "drive",
            "v3",
            credentials=self.credentials,
            cache_discovery=False
        )

        self.retry = retry

        self.cache = {}

        self.root = self._folder(
            DRIVE_ROOT,
            None
        )

        log.info(
            "Google Drive connected. Root: %s",
            DRIVE_ROOT
        )


    # ========================================================
    # INTERNAL RETRY
    # ========================================================

    def _call(
        self,
        function,
        operation="drive"
    ):

        return self.retry(
            function,
            operation
        )


    # ========================================================
    # FOLDER
    # ========================================================

    def _folder(
        self,
        name,
        parent
    ):

        key = (
            name,
            parent
        )

        if key in self.cache:
            return self.cache[key]

        escaped_name = (
            name.replace(
                "'",
                "''"
            )
        )

        query = (
            f"name='{escaped_name}' "
            f"and mimeType='{FOLDER_MIME}' "
            f"and trashed=false"
        )

        if parent:
            query += (
                f" and '{parent}' in parents"
            )

        response = self._call(
            lambda: self.svc.files().list(
                q=query,
                spaces="drive",
                fields="files(id,name)",
                pageSize=100
            ).execute(),
            "drive"
        )

        files = response.get(
            "files",
            []
        )

        if files:

            folder_id = files[0]["id"]

        else:

            body = {
                "name": name,
                "mimeType": FOLDER_MIME
            }

            if parent:
                body[
                    "parents"
                ] = [parent]

            folder_id = self._call(
                lambda: self.svc.files().create(
                    body=body,
                    fields="id"
                ).execute(),
                "drive"
            )["id"]

            log.info(
                "Created Drive folder: %s",
                name
            )

        self.cache[
            key
        ] = folder_id

        return folder_id


    # ========================================================
    # FOLDER PATH
    # ========================================================

    def folder_path(
        self,
        path
    ):

        parent = self.root

        parts = [
            part
            for part in str(path).split("/")
            if part
        ]

        for part in parts:

            parent = self._folder(
                part,
                parent
            )

        return parent


    # ========================================================
    # FIND FILE
    # ========================================================

    def _file(
        self,
        name,
        parent
    ):

        escaped_name = (
            name.replace(
                "'",
                "''"
            )
        )

        query = (
            f"name='{escaped_name}' "
            f"and '{parent}' in parents "
            f"and trashed=false"
        )

        response = self._call(
            lambda: self.svc.files().list(
                q=query,
                spaces="drive",
                fields=(
                    "files("
                    "id,"
                    "name,"
                    "mimeType,"
                    "size,"
                    "md5Checksum"
                    ")"
                ),
                pageSize=100
            ).execute(),
            "drive"
        )

        files = response.get(
            "files",
            []
        )

        return (
            files[0]
            if files
            else None
        )


    # ========================================================
    # LIST FOLDER
    # ========================================================

    def list_folder(
        self,
        remote_path
    ):

        parent = self.folder_path(
            remote_path
        )

        query = (
            f"'{parent}' in parents "
            f"and trashed=false"
        )

        files = []
        token = None

        while True:

            response = self._call(
                lambda: self.svc.files().list(
                    q=query,
                    spaces="drive",
                    fields=(
                        "nextPageToken,"
                        "files("
                        "id,"
                        "name,"
                        "mimeType,"
                        "size,"
                        "md5Checksum"
                        ")"
                    ),
                    pageSize=1000,
                    orderBy="name",
                    pageToken=token
                ).execute(),
                "drive"
            )

            files.extend(
                response.get(
                    "files",
                    []
                )
            )

            token = response.get(
                "nextPageToken"
            )

            if not token:
                break

        return files


    # ========================================================
    # UPLOAD FILE
    # ========================================================

    def put_file(
        self,
        local,
        remote,
        mimetype="application/octet-stream"
    ):

        local = Path(
            local
        )

        if not local.exists():

            raise FileNotFoundError(
                f"Local file does not exist: {local}"
            )

        parts = [
            part
            for part in str(remote).split("/")
            if part
        ]

        if not parts:

            raise ValueError(
                "Remote path cannot be empty."
            )

        name = parts[-1]

        parent_path = "/".join(
            parts[:-1]
        )

        parent = self.folder_path(
            parent_path
        )

        existing = self._file(
            name,
            parent
        )

        size = local.stat().st_size


        def upload(
            request_factory
        ):

            for attempt in range(
                1,
                self.retry.n + 1
            ):

                try:

                    request = request_factory()

                    response = None

                    while response is None:

                        _, response = (
                            request.next_chunk()
                        )

                    return response

                except Exception as exc:

                    # ------------------------------------------------
                    # Check whether upload actually succeeded.
                    # This prevents duplicate Drive files when the
                    # network dies immediately after Google's server
                    # accepted the upload.
                    # ------------------------------------------------

                    found = self._file(
                        name,
                        parent
                    )

                    if found:

                        try:

                            found_size = int(
                                found.get(
                                    "size",
                                    -1
                                )
                            )

                        except (
                            TypeError,
                            ValueError
                        ):

                            found_size = -1

                        if (
                            found_size == size
                            and found.get(
                                "md5Checksum"
                            )
                            == self._md5(
                                local
                            )
                        ):

                            log.info(
                                "Drive upload already "
                                "completed: %s",
                                remote
                            )

                            return found

                    if (
                        attempt
                        >= self.retry.n
                    ):

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
                        "Drive upload failed "
                        "(%s). Retrying in %.1fs",
                        exc,
                        delay
                    )

                    time.sleep(
                        delay
                    )

            raise RuntimeError(
                "Drive upload failed."
            )


        media = lambda: MediaFileUpload(
            str(local),
            mimetype=mimetype,
            chunksize=(
                8 * 1024 * 1024
            ),
            resumable=True
        )


        # --------------------------------------------------------
        # UPDATE EXISTING FILE
        # --------------------------------------------------------

        if existing:

            return upload(
                lambda: self.svc.files().update(
                    fileId=existing["id"],
                    media_body=media(),
                    fields=(
                        "id,"
                        "name,"
                        "size,"
                        "md5Checksum"
                    )
                )
            )


        # --------------------------------------------------------
        # CREATE NEW FILE
        # --------------------------------------------------------

        return upload(
            lambda: self.svc.files().create(
                body={
                    "name": name,
                    "parents": [
                        parent
                    ]
                },
                media_body=media(),
                fields=(
                    "id,"
                    "name,"
                    "size,"
                    "md5Checksum"
                )
            )
        )


    # ========================================================
    # MD5
    # ========================================================

    @staticmethod
    def _md5(
        path
    ):

        digest = hashlib.md5()

        with Path(path).open(
            "rb"
        ) as handle:

            for chunk in iter(
                lambda:
                    handle.read(
                        8 * 1024 * 1024
                    ),
                b""
            ):

                digest.update(
                    chunk
                )

        return digest.hexdigest()


    # ========================================================
    # UPLOAD BYTES / FILE ALIAS
    # ========================================================

    def put_bytes(
        self,
        local,
        remote
    ):

        return self.put_file(
            local,
            remote
        )


    # ========================================================
    # DOWNLOAD BY FILE ID
    # ========================================================

    def download_file(
        self,
        file_id,
        local
    ):

        output = Path(
            local
        )

        output.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        partial = Path(
            str(output)
            + ".part"
        )


        def download():

            request = (
                self.svc.files()
                .get_media(
                    fileId=file_id
                )
            )

            with partial.open(
                "wb"
            ) as handle:

                downloader = (
                    MediaIoBaseDownload(
                        handle,
                        request,
                        chunksize=(
                            8 * 1024 * 1024
                        )
                    )
                )

                done = False

                while not done:

                    _, done = (
                        downloader.next_chunk()
                    )

            partial.replace(
                output
            )

            return output


        try:

            return self._call(
                download,
                "drive"
            )

        except Exception:

            # Never leave a corrupt partial file
            # after a failed download.
            try:
                partial.unlink()
            except OSError:
                pass

            raise


    # ========================================================
    # DOWNLOAD BY REMOTE PATH
    # ========================================================

    def download_file_by_remote(
        self,
        remote,
        local
    ):

        parts = [
            part
            for part in str(remote).split("/")
            if part
        ]

        if not parts:

            raise ValueError(
                "Remote path cannot be empty."
            )

        name = parts[-1]

        parent = self.folder_path(
            "/".join(
                parts[:-1]
            )
        )

        item = self._file(
            name,
            parent
        )

        if not item:

            raise FileNotFoundError(
                remote
            )

        return self.download_file(
            item["id"],
            local
        )


    # ========================================================
    # GET BYTES
    # ========================================================

    def get_bytes(
        self,
        remote
    ):

        parts = [
            part
            for part in str(remote).split("/")
            if part
        ]

        if not parts:

            raise ValueError(
                "Remote path cannot be empty."
            )

        name = parts[-1]

        parent = self.folder_path(
            "/".join(
                parts[:-1]
            )
        )

        item = self._file(
            name,
            parent
        )

        if not item:

            raise FileNotFoundError(
                remote
            )

        output = io.BytesIO()


        def download():

            request = (
                self.svc.files()
                .get_media(
                    fileId=item["id"]
                )
            )

            downloader = (
                MediaIoBaseDownload(
                    output,
                    request,
                    chunksize=(
                        8 * 1024 * 1024
                    )
                )
            )

            done = False

            while not done:

                _, done = (
                    downloader.next_chunk()
                )

            return output.getvalue()


        return self._call(
            download,
            "drive"
        )


    # ========================================================
    # DELETE FILE
    # ========================================================

    def delete(
        self,
        remote
    ):

        parts = [
            part
            for part in str(remote).split("/")
            if part
        ]

        if not parts:
            return

        name = parts[-1]

        parent = self.folder_path(
            "/".join(
                parts[:-1]
            )
        )

        item = self._file(
            name,
            parent
        )

        if item:

            self._call(
                lambda:
                    self.svc.files()
                    .delete(
                        fileId=item["id"]
                    )
                    .execute(),
                "drive"
            )


    # ========================================================
    # DELETE ENTIRE TREE
    # ========================================================

    def delete_tree(
        self,
        remote_path
    ):

        items = self.list_folder(
            remote_path
        )

        for item in items:

            child = (
                f"{remote_path.rstrip('/')}"
                f"/{item['name']}"
            )

            if (
                item.get(
                    "mimeType"
                )
                == FOLDER_MIME
            ):

                self.delete_tree(
                    child
                )

                self._delete_id(
                    item["id"]
                )

            else:

                self._delete_id(
                    item["id"]
                )


    # ========================================================
    # DELETE BY ID
    # ========================================================

    def _delete_id(
        self,
        file_id
    ):

        self._call(
            lambda:
                self.svc.files()
                .delete(
                    fileId=file_id
                )
                .execute(),
            "drive"
        )
