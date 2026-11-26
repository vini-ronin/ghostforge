"""Autoriza o Drive e cria a pasta privada com o mesmo aplicativo OAuth."""
import argparse
import os
from pathlib import Path

import requests
from google_auth_oauthlib.flow import InstalledAppFlow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Escolha um arquivo de saída novo, fora do repositório.")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(args.client), ["https://www.googleapis.com/auth/drive.file"],
    )
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        raise SystemExit("O Google não devolveu refresh token.")
    response = requests.post(
        "https://www.googleapis.com/drive/v3/files", params={"fields": "id"},
        headers={"Authorization": "Bearer " + credentials.token},
        json={"name": "GhostForge", "mimeType": "application/vnd.google-apps.folder"}, timeout=30,
    )
    response.raise_for_status()
    values = {
        "GOOGLE_CLIENT_ID": credentials.client_id, "GOOGLE_CLIENT_SECRET": credentials.client_secret,
        "GOOGLE_REFRESH_TOKEN": credentials.refresh_token, "GOOGLE_DRIVE_FOLDER_ID": response.json()["id"],
    }
    fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        for name, value in values.items():
            target.write(f"{name}={value}\n")
    print("Pasta criada e configuração privada salva.")


if __name__ == "__main__":
    main()
