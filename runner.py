"""Executado no Actions. O vídeo nunca vira artefato do GitHub."""
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import requests

from videoeditor.render_command import build


def main():
    job = os.environ["RENDER_JOB"]
    if not re.fullmatch(r"[a-f0-9]{32}", job):
        raise SystemExit("Identificador inválido.")
    base = os.environ["GHOSTFORGE_URL"].rstrip("/")
    if urlsplit(base).scheme != "https":
        raise SystemExit("Configure a URL pública HTTPS.")
    root = f"{base}/runner/jobs/{job}"
    headers = {"Authorization": "Bearer " + os.environ["GHOSTFORGE_RUNNER_TOKEN"]}

    def call(suffix, payload=None):
        response = requests.post(root + suffix, headers=headers, json=payload, timeout=40)
        if response.status_code == 409 and suffix == "/claim":
            return None
        response.raise_for_status()
        return response.json()

    def download(asset, path):
        with requests.get(root + f"/asset/{asset}", headers=headers, stream=True, timeout=(15, 120)) as response, path.open("wb") as file:
            response.raise_for_status()
            for block in response.iter_content(1024 * 1024):
                file.write(block)

    claimed = False
    try:
        options = call("/claim")
        if options is None:
            return
        claimed = True
        with tempfile.TemporaryDirectory(prefix="ghostforge-") as root_dir:
            folder = Path(root_dir)
            source, output = folder / "video", folder / "output.mp4"
            template = folder / "template.png" if options.get("template") else None
            download("video", source)
            if template:
                download("template", template)
            subprocess.run(build(source, template, output, options["clip"], options["render"]),
                           check=True, timeout=35 * 60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            url = call("/upload", {"size": output.stat().st_size})["url"]
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com":
                raise ValueError("Origem de upload inesperada.")
            with output.open("rb") as file:
                result = requests.put(url, data=file, headers={"Content-Type": "video/mp4"}, timeout=(15, 300))
            result.raise_for_status()
            call("/complete", {"file_id": result.json()["id"]})
    except Exception:
        if claimed:
            try:
                call("/fail")
            except requests.RequestException:
                pass
        raise SystemExit("Falha no render ou na transferência. Consulte o lote no GhostForge.") from None


if __name__ == "__main__":
    main()
