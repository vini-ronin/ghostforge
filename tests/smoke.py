"""Fluxo de integração com mídia sintética e provedores isolados."""
import importlib
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from cloud_auth import password_hash
from videoeditor.render_command import build


def main():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        salt = bytes(range(16))
        password = "fixture-only-password"
        environment = {
            "GHOSTFORGE_MODE": "production", "GHOSTFORGE_EMAIL": "fixture@example.test",
            "GHOSTFORGE_PASSWORD_HASH": salt.hex() + ":" + password_hash(password, salt),
            "GHOSTFORGE_SESSION_SECRET": "fixture-session-key", "GHOSTFORGE_API_TOKEN": "fixture-api",
            "GHOSTFORGE_RUNNER_TOKEN": "fixture-runner", "GHOSTFORGE_PUBLIC_URL": "https://testserver",
            "GITHUB_RENDER_REPO": "fixture/repo", "GITHUB_RENDER_TOKEN": "fixture-token",
            "GOOGLE_CLIENT_ID": "fixture", "GOOGLE_CLIENT_SECRET": "fixture",
            "GOOGLE_REFRESH_TOKEN": "fixture", "GOOGLE_DRIVE_FOLDER_ID": "fixture",
            "GHOSTFORGE_DATA": str(root / "jobs"), "REELS_EDITOR_INPUT": str(root / "input"),
            "REELS_EDITOR_OUTPUT": str(root / "output"), "REELS_DOWNLOAD_DIR": str(root / "downloads"),
            "REELS_SESSION_FILE": str(root / "session.json"),
        }
        with patch.dict(os.environ, environment):
            app = importlib.import_module("local").app
            worker = importlib.import_module("cloud_worker")
            source = root / "input" / "Fixture.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=c=navy:s=180x320:r=24:d=0.75", str(source)], check=True)
            with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
                assert client.get("/").status_code == 303
                assert client.get("/api/state").status_code == 401
                assert client.get("/reels/api/session/status").status_code == 401
                assert client.get("/input/Fixture.mp4").status_code == 401
                login = client.post("/login", data={"email": "fixture@example.test", "password": password})
                assert login.status_code == 303 and "HttpOnly" in login.headers["set-cookie"]
                assert client.get("/").status_code == 200
                assert client.get("/api/process_stream").status_code == 404
                assert client.post("/api/delete_video", params={"filename": "../session.json"}).status_code == 422
                client.cookies.clear()
                api = {"Authorization": "Bearer fixture-api"}
                runner = {"Authorization": "Bearer fixture-runner"}
                files = client.get("/api/state", headers=api).json()
                assert files["videos"] == ["Fixture.mp4"]
                batch = client.post("/api/jobs", headers=api, json={
                    "videos": [{"filename": "Fixture.mp4"}, {"filename": "Fixture.mp4", "flip": True}],
                    "global_quality": "720p",
                })
                assert batch.status_code == 202, batch.text
                identifier = batch.json()["batch_id"]
                with (
                    patch("cloud_worker.drive.headers", return_value={}),
                    patch("cloud_worker.drive.upload", return_value="mock-source-id") as upload,
                    patch("cloud_worker.dispatch") as dispatch,
                ):
                    assert worker.step()
                    assert upload.call_count == 1
                    assert len(dispatch.call_args.args[1]) == 2
                queued = client.get(f"/api/jobs/{identifier}", headers=api).json()
                assert queued["state"] == "dispatched"
                assert client.post(f'/runner/jobs/{queued["jobs"][0]["id"]}/claim', headers=api).status_code == 401
                for job in queued["jobs"]:
                    endpoint = "/runner/jobs/" + job["id"]
                    options = client.post(endpoint + "/claim", headers=runner)
                    assert options.status_code == 200, options.text
                    assert client.post(endpoint + "/claim", headers=runner).status_code == 409
                    if job == queued["jobs"][0]:
                        rendered = root / "render.mp4"
                        data = options.json()
                        subprocess.run(build(source, None, rendered, data["clip"], data["render"]), check=True)
                        assert rendered.stat().st_size > 1000
                    with patch("cloud_jobs.drive.metadata", return_value={"appProperties": {"ghostforgeJob": job["id"]}}):
                        response = client.post(endpoint + "/complete", headers=runner, json={"file_id": "mock-output-id"})
                        assert response.status_code == 200
                assert client.get(f"/api/jobs/{identifier}", headers=api).json()["state"] == "done"
    print("Login, autorização, fila, Drive simulado, matriz, callbacks e FFmpeg verificados.")


if __name__ == "__main__":
    main()
