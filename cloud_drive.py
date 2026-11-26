"""Transferências privadas para o Google Drive."""
import mimetypes
import threading
import time
from pathlib import Path

import requests

from cloud_auth import setting

FILES = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
_token_lock = threading.Lock()
_token = ""
_expires_at = 0.0


def headers() -> dict:
    global _token, _expires_at
    with _token_lock:
        now = time.monotonic()
        if now >= _expires_at:
            response = requests.post("https://oauth2.googleapis.com/token", data={
                "client_id": setting("GOOGLE_CLIENT_ID"), "client_secret": setting("GOOGLE_CLIENT_SECRET"),
                "refresh_token": setting("GOOGLE_REFRESH_TOKEN"), "grant_type": "refresh_token",
            }, timeout=15)
            response.raise_for_status()
            data = response.json()
            _token = data["access_token"]
            _expires_at = now + max(0, int(data.get("expires_in", 0)) - 60)
        return {"Authorization": "Bearer " + _token}


def upload_session(name: str, mime: str, size: int, auth: dict, properties: dict | None = None) -> str:
    data = {"name": name}
    if folder := setting("GOOGLE_DRIVE_FOLDER_ID"):
        data["parents"] = [folder]
    if properties:
        data["appProperties"] = properties
    response = requests.post(UPLOAD, params={"uploadType": "resumable", "fields": "id,name,size"},
        json=data, headers={**auth, "X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)}, timeout=25)
    response.raise_for_status()
    return response.headers["Location"]


def send_file(url: str, path: Path, mime: str) -> dict:
    with path.open("rb") as source:
        response = requests.put(url, data=source, headers={
            "Content-Type": mime, "Content-Length": str(path.stat().st_size),
        }, timeout=(15, 240))
    response.raise_for_status()
    return response.json()


def upload(path: Path, name: str, auth: dict) -> str:
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return send_file(upload_session(name, mime, path.stat().st_size, auth), path, mime)["id"]


def open_file(file_id: str, headers_override: dict | None = None):
    response = requests.get(f"{FILES}/{file_id}", params={"alt": "media"}, headers={
        **headers(), **(headers_override or {}),
    }, stream=True, timeout=(15, 90))
    if response.status_code not in {200, 206}:
        response.close()
        response.raise_for_status()
    return response


def metadata(file_id: str) -> dict:
    response = requests.get(f"{FILES}/{file_id}", params={"fields": "id,name,size,appProperties,trashed"},
                            headers=headers(), timeout=15)
    response.raise_for_status()
    return response.json()
