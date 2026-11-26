"""Fila persistente e protocolo entre GhostForge, Drive e Actions."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StringConstraints, model_validator

import cloud_drive as drive
from cloud_auth import setting
from videoeditor.app import INPUT_DIR, video_path

router = APIRouter()
ID = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
DRIVE_ID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,128}$")]
DATA = Path(os.environ.get("GHOSTFORGE_DATA", Path(__file__).parent / "cloud-data"))
DB_FILE = DATA / "queue.sqlite3"
MEDIA = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
MAX_BYTES = 3 * 1024**3


class Clip(BaseModel):
    filename: str = Field(min_length=1, max_length=220)
    template_path: str | None = Field(default=None, max_length=250)
    x_offset: int = Field(default=0, ge=-4000, le=4000)
    y_offset: int = Field(default=0, ge=-4000, le=4000)
    scale: float = Field(default=1, ge=.05, le=3)
    crop_t: int = Field(default=0, ge=0, le=98)
    crop_b: int = Field(default=0, ge=0, le=98)
    crop_l: int = Field(default=0, ge=0, le=98)
    crop_r: int = Field(default=0, ge=0, le=98)
    comp_mode: Literal["overlay", "crop_template"] = "overlay"
    bg_color: Annotated[str, StringConstraints(pattern=r"^#[a-fA-F0-9]{6}$")] = "#000000"
    no_border: bool = False
    flip: bool = False
    delete: bool = False

    @model_validator(mode="after")
    def valid_crop(self):
        if self.crop_t + self.crop_b >= 100 or self.crop_l + self.crop_r >= 100:
            raise ValueError("Recorte elimina todo o vídeo.")
        return self


class Batch(BaseModel):
    videos: list[Clip] = Field(min_length=1, max_length=40)
    global_quality: Literal["720p", "1080p"] = "1080p"
    global_no_border: bool = False
    global_audio_enhance: bool = False


class UploadRequest(BaseModel):
    size: int = Field(ge=1, le=MAX_BYTES)


class Completion(BaseModel):
    file_id: DRIVE_ID


class DriveImport(BaseModel):
    file_id: DRIVE_ID
    filename: str = Field(min_length=1, max_length=200)


@contextmanager
def database():
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, state TEXT, created INTEGER, updated INTEGER, error TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, batch TEXT, name TEXT, config TEXT, state TEXT,
          input_id TEXT, template_id TEXT, output_id TEXT, error TEXT, updated INTEGER);
        CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch);
        CREATE TABLE IF NOT EXISTS assets(hash TEXT PRIMARY KEY, file_id TEXT NOT NULL);
        """)


def media_path(name: str, extensions: set) -> Path:
    if name != Path(name).name or any(x in name for x in ("/", "\\", "\0")) or Path(name).suffix.lower() not in extensions:
        raise HTTPException(422, "Nome ou formato de arquivo inválido.")
    base = Path(INPUT_DIR).resolve()
    path = (base / name).resolve()
    if path.parent != base:
        raise HTTPException(422, "Caminho inválido.")
    return path


def template_for(clip: Clip) -> Path | None:
    if clip.template_path:
        url = urlsplit(clip.template_path)
        if url.netloc or url.scheme or not url.path.startswith("/input/"):
            raise HTTPException(422, "O template deve vir da entrada do editor.")
        path = media_path(unquote(url.path.removeprefix("/input/")), {".png", ".jpg", ".jpeg"})
        if not path.is_file():
            raise HTTPException(422, "Template indicado indisponível.")
    else:
        path = media_path("template.png", {".png"})
    return path if path.is_file() else None


@router.post("/api/jobs", status_code=202)
def submit(batch: Batch):
    selected = []
    total = 0
    for clip in batch.videos:
        if clip.delete:
            continue
        source, template = media_path(clip.filename, MEDIA), template_for(clip)
        if not source.is_file():
            raise HTTPException(404, "Um vídeo da fila não está na pasta de entrada.")
        total += source.stat().st_size + (template.stat().st_size if template else 0)
        selected.append((clip, source, template))
    if not selected or total > MAX_BYTES:
        raise HTTPException(422, "Selecione vídeos com até 3 GiB no total por lote.")
    identifier, stamp = uuid4().hex, int(time.time())
    scratch = DATA / "staging" / identifier
    scratch.mkdir(parents=True)
    items = []
    try:
        for clip, source, template in selected:
            job_id = uuid4().hex
            job_dir = scratch / job_id
            job_dir.mkdir()
            shutil.copyfile(source, job_dir / "video")
            if template:
                shutil.copyfile(template, job_dir / "template")
            options = {"clip": clip.model_dump(), "render": batch.model_dump(exclude={"videos"})}
            if template:
                options["template_name"] = template.name
            items.append((job_id, identifier, source.name, json.dumps(options), "waiting", stamp))
        with database() as db:
            db.execute("INSERT INTO batches VALUES (?, 'queued', ?, ?, NULL)", (identifier, stamp, stamp))
            db.executemany("INSERT INTO jobs(id,batch,name,config,state,updated) VALUES (?,?,?,?,?,?)", items)
    except (OSError, sqlite3.Error):
        shutil.rmtree(scratch, ignore_errors=True)
        raise HTTPException(503, "Não foi possível preparar o lote.") from None
    return {"batch_id": identifier, "state": "queued", "count": len(items)}


@router.get("/api/jobs")
def list_batches():
    with database() as db:
        return [dict(r) for r in db.execute("SELECT * FROM batches ORDER BY created DESC, rowid DESC LIMIT 50")]


@router.get("/api/jobs/{batch_id}")
def status(batch_id: ID):
    with database() as db:
        batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise HTTPException(404, "Lote não encontrado.")
        jobs = db.execute("SELECT id,name,state,error,output_id FROM jobs WHERE batch=? ORDER BY rowid", (batch_id,))
        return {**dict(batch), "jobs": [
            {"id": job["id"], "name": job["name"], "state": job["state"], "error": job["error"],
             "output_url": f'/api/render-output/{job["id"]}' if job["output_id"] else None}
            for job in jobs
        ]}


def job_row(db, job_id):
    job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise HTTPException(404, "Render não encontrado.")
    return job


def refresh_batch(db, batch):
    pending = db.execute("SELECT count(*) FROM jobs WHERE batch=? AND state IN ('waiting','running')", (batch,)).fetchone()[0]
    if not pending:
        errors = db.execute("SELECT count(*) FROM jobs WHERE batch=? AND state='failed'", (batch,)).fetchone()[0]
        db.execute("UPDATE batches SET state=?,updated=? WHERE id=?", ("partial" if errors else "done", int(time.time()), batch))


@router.post("/runner/jobs/{job_id}/claim")
def claim(job_id: ID):
    with database() as db:
        job = job_row(db, job_id)
        if job["state"] != "waiting" or not job["input_id"]:
            raise HTTPException(409, "Render já atendido ou indisponível.")
        changed = db.execute("UPDATE jobs SET state='running',updated=? WHERE id=? AND state='waiting'",
                             (int(time.time()), job_id)).rowcount
        if not changed:
            raise HTTPException(409, "Render já atendido.")
        options = json.loads(job["config"])
        return {**options, "template": bool(job["template_id"])}


def stream_drive(file_id: str, range_header: str | None = None):
    try:
        response = drive.open_file(file_id, {"Range": range_header} if range_header else {})
    except requests.RequestException:
        raise HTTPException(502, "Arquivo indisponível no Drive.") from None

    def body():
        with response:
            yield from response.iter_content(1024 * 1024)

    headers = {k: response.headers[k] for k in ("Content-Length", "Content-Range", "Accept-Ranges") if k in response.headers}
    headers["Cache-Control"] = "private, no-store"
    return StreamingResponse(body(), status_code=response.status_code, headers=headers,
                             media_type=response.headers.get("Content-Type", "application/octet-stream"))


@router.get("/runner/jobs/{job_id}/asset/{asset}")
def runner_asset(job_id: ID, asset: Literal["video", "template"]):
    with database() as db:
        job = job_row(db, job_id)
    file_id = job["input_id" if asset == "video" else "template_id"]
    if job["state"] != "running" or not file_id:
        raise HTTPException(404, "Arquivo indisponível.")
    return stream_drive(file_id)


@router.post("/runner/jobs/{job_id}/upload")
def begin_upload(job_id: ID, info: UploadRequest):
    with database() as db:
        job = job_row(db, job_id)
    if job["state"] != "running":
        raise HTTPException(409, "Render fora de execução.")
    try:
        return {"url": drive.upload_session(f"forged-{job_id}.mp4", "video/mp4", info.size, drive.headers(), {"ghostforgeJob": job_id})}
    except requests.RequestException:
        raise HTTPException(502, "Drive recusou o upload.") from None


@router.post("/runner/jobs/{job_id}/complete")
def complete(job_id: ID, info: Completion):
    try:
        metadata = drive.metadata(info.file_id)
        if metadata.get("trashed") or metadata.get("appProperties", {}).get("ghostforgeJob") != job_id:
            raise HTTPException(422, "O arquivo do Drive não pertence a este render.")
    except requests.RequestException:
        raise HTTPException(502, "O Drive não confirmou o arquivo.") from None
    with database() as db:
        job = job_row(db, job_id)
        if job["state"] == "done" and job["output_id"] == info.file_id:
            return {"status": "done"}
        if job["state"] != "running":
            raise HTTPException(409, "Render fora de execução.")
        db.execute("UPDATE jobs SET state='done',output_id=?,updated=? WHERE id=? AND state='running'",
                   (info.file_id, int(time.time()), job_id))
        refresh_batch(db, job["batch"])
    return {"status": "done"}


@router.post("/runner/jobs/{job_id}/fail")
def fail(job_id: ID):
    with database() as db:
        job = job_row(db, job_id)
        db.execute("UPDATE jobs SET state='failed',error='Falha no runner.',updated=? WHERE id=? AND state='running'",
                   (int(time.time()), job_id))
        refresh_batch(db, job["batch"])
    return {"status": "received"}


@router.get("/api/render-output/{job_id}")
def output(job_id: ID, request: Request):
    with database() as db:
        file_id = job_row(db, job_id)["output_id"]
    if not file_id:
        raise HTTPException(404, "Saída indisponível.")
    response = stream_drive(file_id, request.headers.get("range"))
    response.headers["Content-Disposition"] = f'attachment; filename="ghostforge-{job_id}.mp4"'
    return response


@router.post("/api/drive/import", status_code=201)
def import_drive(info: DriveImport):
    target = video_path(info.filename)
    temp = target.with_name("." + uuid4().hex + ".part")
    try:
        with drive.open_file(info.file_id) as response, temp.open("xb") as destination:
            size = 0
            for chunk in response.iter_content(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, "Arquivo grande demais.")
                destination.write(chunk)
        if target.exists():
            raise HTTPException(409, "Já existe um vídeo com esse nome.")
        os.link(temp, target)
    except FileExistsError:
        raise HTTPException(409, "Já existe um vídeo com esse nome.") from None
    except (OSError, requests.RequestException):
        raise HTTPException(502, "O arquivo não pôde ser importado.") from None
    finally:
        temp.unlink(missing_ok=True)
    return {"filename": target.name, "size": size}


def cache_in_drive(path: Path, name: str, auth: dict) -> str:
    with path.open("rb") as source:
        fingerprint = hashlib.file_digest(source, "sha256").hexdigest()
    with database() as db:
        cached = db.execute("SELECT file_id FROM assets WHERE hash=?", (fingerprint,)).fetchone()
    if cached:
        return cached["file_id"]
    file_id = drive.upload(path, name, auth)
    with database() as db:
        db.execute("INSERT OR REPLACE INTO assets VALUES (?,?)", (fingerprint, file_id))
    return file_id
