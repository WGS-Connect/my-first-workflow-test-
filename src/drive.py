from __future__ import annotations
import io, json, logging, hashlib
from pathlib import Path
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from .config import DRIVE_ROOT

log = logging.getLogger("audiobook")
SCOPES = ["https://www.googleapis.com/auth/drive"]

class Drive:
    def __init__(self, credentials_json, retry):
        info = json.loads(credentials_json)
        self.svc = build(
            "drive", "v3",
            credentials=Credentials.from_service_account_info(info, scopes=SCOPES),
            cache_discovery=False
        )
        self.retry = retry
        self.cache = {}
        self.root = self._folder(DRIVE_ROOT, None)

    def _folder(self, name, parent):
        key = (name, parent)
        if key in self.cache:
            return self.cache[key]
        q = f"name='{name.replace(chr(39), chr(39)*2)}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent:
            q += f" and '{parent}' in parents"
        r = self.retry(lambda: self.svc.files().list(
            q=q, spaces="drive", fields="files(id,name)", pageSize=100
        ).execute(), "drive")
        if r["files"]:
            fid = r["files"][0]["id"]
        else:
            body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
            if parent:
                body["parents"] = [parent]
            fid = self.retry(lambda: self.svc.files().create(
                body=body, fields="id"
            ).execute(), "drive")["id"]
        self.cache[key] = fid
        return fid

    def folder_path(self, path):
        parent = self.root
        for part in [x for x in path.split("/") if x]:
            parent = self._folder(part, parent)
        return parent

    def _file(self, name, parent):
        q = f"name='{name.replace(chr(39), chr(39)*2)}' and '{parent}' in parents and trashed=false"
        r = self.retry(lambda: self.svc.files().list(
            q=q, spaces="drive",
            fields="files(id,name,mimeType,size,md5Checksum)",
            pageSize=100
        ).execute(), "drive")
        return r["files"][0] if r.get("files") else None

    def list_folder(self, remote_path):
        parent = self.folder_path(remote_path)
        q = f"'{parent}' in parents and trashed=false"
        files, token = [], None
        while True:
            r = self.retry(lambda: self.svc.files().list(
                q=q, spaces="drive",
                fields="nextPageToken,files(id,name,mimeType,size,md5Checksum)",
                pageSize=1000, orderBy="name",
                pageToken=token
            ).execute(), "drive")
            files.extend(r.get("files", []))
            token = r.get("nextPageToken")
            if not token:
                break
        return files

    def put_file(self, local, remote, mimetype="application/octet-stream"):
        """Streaming/resumable upload. Never reads the whole file into RAM."""
        local = Path(local)
        parts = remote.split("/")
        name = parts[-1]
        parent = self.folder_path("/".join(parts[:-1]))
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
                    # If a create request succeeded but its final response was lost,
                    # do not create a second copy on the next attempt.
                    found = self._file(name, parent)
                    if found and found.get("size") and int(found["size"]) == local.stat().st_size:
                        if found.get("md5Checksum") == self._md5(local):
                            return found
                    if attempt == self.retry.n:
                        raise
                    delay = self.retry.lo + (self.retry.hi - self.retry.lo) * random.random()
                    log.warning("Drive upload failed: %s; retrying in %.1fs", exc, delay)
                    time.sleep(delay)
            raise RuntimeError("Drive upload failed")

        import random, time
        if existing:
            return upload(lambda: self.svc.files().update(
                fileId=existing["id"],
                media_body=MediaFileUpload(str(local), mimetype=mimetype, chunksize=8 * 1024 * 1024, resumable=True),
                fields="id,size,md5Checksum"
            ))
        return upload(lambda: self.svc.files().create(
            body={"name": name, "parents": [parent]},
            media_body=MediaFileUpload(str(local), mimetype=mimetype, chunksize=8 * 1024 * 1024, resumable=True),
            fields="id,size,md5Checksum"
        ))

    @staticmethod
    def _md5(path):
        h = hashlib.md5()
        with Path(path).open("rb") as f:
            for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def put_bytes(self, local, remote):
        return self.put_file(local, remote)

    def download_file(self, file_id, local):
        out = Path(local)
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_suffix(out.suffix + ".part")

        def dl():
            req = self.svc.files().get_media(fileId=file_id)
            with part.open("wb") as f:
                downloader = MediaIoBaseDownload(f, req, chunksize=8 * 1024 * 1024)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            part.replace(out)
            return out

        self.retry(dl, "drive")
        return out

    def download_file_by_remote(self, remote, local):
        parts = remote.split("/")
        parent = self.folder_path("/".join(parts[:-1]))
        item = self._file(parts[-1], parent)
        if not item:
            raise FileNotFoundError(remote)
        return self.download_file(item["id"], local)

    def get_bytes(self, remote):
        parts = remote.split("/")
        parent = self.folder_path("/".join(parts[:-1]))
        item = self._file(parts[-1], parent)
        if not item:
            raise FileNotFoundError(remote)
        out = io.BytesIO()
        def dl():
            req = self.svc.files().get_media(fileId=item["id"])
            downloader = MediaIoBaseDownload(out, req, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return out.getvalue()
        return self.retry(dl, "drive")

    def delete(self, remote):
        parts = remote.split("/")
        parent = self.folder_path("/".join(parts[:-1]))
        item = self._file(parts[-1], parent)
        if item:
            self.retry(lambda: self.svc.files().delete(fileId=item["id"]).execute(), "drive")

    def delete_tree(self, remote_path):
        items = self.list_folder(remote_path)
        for item in items:
            child = f"{remote_path.rstrip('/')}/{item['name']}"
            if item.get("mimeType") == "application/vnd.google-apps.folder":
                self.delete_tree(child)
                self._delete_id(item["id"])
            else:
                self._delete_id(item["id"])

    def _delete_id(self, file_id):
        self.retry(lambda: self.svc.files().delete(fileId=file_id).execute(), "drive")
