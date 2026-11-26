"""Servidor MCP por stdio para o GhostForge da VPS."""
import os
import re

import httpx
from mcp.server.fastmcp import FastMCP

from cloud_jobs import Batch, DriveImport

server = FastMCP("GhostForge")


async def call(method, path, payload=None):
    url = os.environ["GHOSTFORGE_PUBLIC_URL"].rstrip("/")
    if not url.startswith("https://"):
        raise ValueError("Configure a URL HTTPS do GhostForge.")
    async with httpx.AsyncClient(
        base_url=url, headers={"Authorization": "Bearer " + os.environ["GHOSTFORGE_API_TOKEN"]}, timeout=300,
    ) as client:
        response = await client.request(method, path, json=payload)
        if response.is_error:
            raise ValueError(f"GhostForge devolveu HTTP {response.status_code}.")
        return response.json()


@server.tool()
async def submit_render(batch: Batch) -> dict:
    """Enfileira um vídeo por runner na matriz do GitHub Actions."""
    return await call("POST", "/api/jobs", batch.model_dump())


@server.tool()
async def render_status(batch_id: str) -> dict:
    """Consulta o lote e obtém os caminhos autenticados dos arquivos finalizados."""
    if not re.fullmatch(r"[a-f0-9]{32}", batch_id):
        raise ValueError("ID inválido.")
    return await call("GET", "/api/jobs/" + batch_id)


@server.tool()
async def list_batches() -> list[dict]:
    """Lista lotes recentes."""
    return await call("GET", "/api/jobs")


@server.tool()
async def list_inputs() -> dict:
    """Lista os vídeos na fila de entrada do editor."""
    return await call("GET", "/api/state")


@server.tool()
async def import_from_drive(media: DriveImport) -> dict:
    """Importa um arquivo do Drive autorizado para a entrada do editor."""
    return await call("POST", "/api/drive/import", media.model_dump())


if __name__ == "__main__":
    server.run(transport="stdio")
