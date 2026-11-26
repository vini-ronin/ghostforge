"""Worker leve. Prepara arquivos e despacha uma matriz no GitHub Actions."""
import json
import shutil
import time

import requests

import cloud_drive as drive
from cloud_auth import setting
from cloud_jobs import DATA, cache_in_drive, database, initialize, refresh_batch


def dispatch(batch_id: str, jobs: list[str]):
    repo = setting("GITHUB_RENDER_REPO")
    response = requests.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/render.yml/dispatches",
        headers={"Authorization": "Bearer " + setting("GITHUB_RENDER_TOKEN"),
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        json={"ref": setting("GITHUB_RENDER_REF") or "main",
              "inputs": {"batch": batch_id, "jobs": json.dumps(jobs, separators=(",", ":"))}},
        timeout=25,
    )
    response.raise_for_status()


def prepare(batch_id: str):
    with database() as db:
        jobs = db.execute("SELECT * FROM jobs WHERE batch=?", (batch_id,)).fetchall()
    scratch = DATA / "staging" / batch_id
    auth = drive.headers()
    for job in jobs:
        path = scratch / job["id"]
        config = json.loads(job["config"])
        original = job["input_id"] or cache_in_drive(path / "video", job["name"], auth)
        template = job["template_id"]
        if not template and config.get("template_name"):
            template = cache_in_drive(path / "template", config["template_name"], auth)
        with database() as db:
            db.execute("UPDATE jobs SET input_id=?,template_id=? WHERE id=?", (original, template, job["id"]))
    with database() as db:
        db.execute("UPDATE batches SET state='dispatching',updated=? WHERE id=?", (int(time.time()), batch_id))
    try:
        dispatch(batch_id, [job["id"] for job in jobs])
    except (requests.Timeout, requests.ConnectionError):
        # Um timeout não distingue recusa de execução aceita.
        return
    with database() as db:
        db.execute("UPDATE batches SET state='dispatched',updated=? WHERE id=? AND state='dispatching'",
                   (int(time.time()), batch_id))


def step() -> bool:
    now = int(time.time())
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE batches SET state='queued' WHERE state='preparing' AND updated<?", (now - 3600,))
        overdue = db.execute("SELECT id FROM batches WHERE state IN ('dispatching','dispatched') AND updated<?",
                             (now - 3 * 3600,)).fetchall()
        closed = []
        for row in overdue:
            identifier = row["id"]
            db.execute("UPDATE jobs SET state='failed',error='Nenhuma resposta do runner dentro do prazo.' "
                       "WHERE batch=? AND state IN ('waiting','running')", (identifier,))
            refresh_batch(db, identifier)
            closed.append(identifier)
        row = db.execute("SELECT id FROM batches WHERE state='queued' ORDER BY created,rowid LIMIT 1").fetchone()
        if row is not None:
            batch = row["id"]
            db.execute("UPDATE batches SET state='preparing',updated=? WHERE id=?", (now, batch))
    for identifier in closed:
        shutil.rmtree(DATA / "staging" / identifier, ignore_errors=True)
    if row is None:
        return False
    try:
        prepare(batch)
    except (requests.RequestException, OSError, ValueError, KeyError) as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        explanation = f"Falha na integração com Drive ou GitHub (HTTP {code})." if code else "Falha ao preparar o lote."
        with database() as db:
            db.execute("UPDATE jobs SET state='failed',error=? WHERE batch=? AND state='waiting'", (explanation, batch))
            db.execute("UPDATE batches SET state='failed',error=?,updated=? WHERE id=?", (explanation, int(time.time()), batch))
    finally:
        shutil.rmtree(DATA / "staging" / batch, ignore_errors=True)
    return True


def main():
    initialize()
    while True:
        if not step():
            time.sleep(3)


if __name__ == "__main__":
    main()
