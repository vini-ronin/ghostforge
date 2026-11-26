"""Inicia o GhostForge com o explorador GhostReel integrado."""
import argparse
from urllib.parse import urlsplit

import uvicorn

import cloud_auth
import cloud_jobs
from ghostreel.app import app as explorer
from videoeditor.app import app

app.state.explorer_path = "/reels/"
app.state.production_mode = cloud_auth.production()
explorer.state.editor_path = "/"
app.mount(app.state.explorer_path.rstrip("/"), explorer, name="explorer")
app.middleware("http")(cloud_auth.guard)
app.include_router(cloud_auth.router)
app.include_router(cloud_jobs.router)


@app.on_event("startup")
def startup():
    if cloud_auth.production():
        required = (
            "GHOSTFORGE_EMAIL", "GHOSTFORGE_PASSWORD_HASH", "GHOSTFORGE_SESSION_SECRET",
            "GHOSTFORGE_API_TOKEN", "GHOSTFORGE_RUNNER_TOKEN", "GHOSTFORGE_PUBLIC_URL",
            "GITHUB_RENDER_REPO", "GITHUB_RENDER_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
            "GOOGLE_REFRESH_TOKEN", "GOOGLE_DRIVE_FOLDER_ID",
        )
        missing = [name for name in required if not cloud_auth.setting(name)]
        if missing:
            raise RuntimeError("Configure: " + ", ".join(missing))
        site = urlsplit(cloud_auth.setting("GHOSTFORGE_PUBLIC_URL"))
        if site.scheme != "https" or not site.hostname or site.username or site.path not in {"", "/"} or site.query or site.fragment:
            raise RuntimeError("GHOSTFORGE_PUBLIC_URL deve ser a origem HTTPS do GhostForge.")
        credentials = [cloud_auth.setting(name) for name in (
            "GHOSTFORGE_SESSION_SECRET", "GHOSTFORGE_API_TOKEN", "GHOSTFORGE_RUNNER_TOKEN",
        )]
        if len(set(credentials)) != len(credentials):
            raise RuntimeError("Use valores distintos para sessão, API e runners.")
    cloud_jobs.initialize()


@app.get("/healthz")
def health():
    return {"status": "ok"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8782)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
