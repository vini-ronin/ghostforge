# GhostForge

Editor em lote com GhostReel integrado, fila de render por API e MCP e execução paralela no GitHub Actions. A VPS prepara e acompanha lotes. Os runners executam FFmpeg e salvam os resultados privados no Drive.

## Implantar no Coolify

1. Importe este repositório como aplicativo **Docker Compose**, branch `main`, caminho `/compose.yaml`. Aponte o domínio HTTPS para o serviço **ghostforge**, porta interna **8782**. O serviço **orchestrator** roda em segundo plano. Preserve o volume `ghostforge-data` e mantenha **uma réplica de cada serviço**. O SQLite usa WAL e os dois containers compartilham o volume.
2. Configure as variáveis abaixo no Coolify. Use os valores apenas no painel de segredos. O health check chama `GET /healthz`.
3. No repositório público usado para render, habilite Actions e cadastre **Settings → Secrets and variables → Actions**. Crie a variável `GHOSTFORGE_URL` com a URL HTTPS e o secret `GHOSTFORGE_RUNNER_TOKEN` com o mesmo valor da VPS. O workflow fica em `.github/workflows/render.yml` no branch `main`.
4. Abra o domínio, entre com a conta configurada, importe um vídeo curto e envie um lote. Confirme o estado no painel **Lotes no GitHub Actions**, depois baixe a saída. Faça a primeira prova com material próprio.

| Variável no Coolify | Valor |
| --- | --- |
| `GHOSTFORGE_PUBLIC_URL` | URL HTTPS do domínio, sem caminho. A mesma URL em `GHOSTFORGE_URL` no GitHub. |
| `GHOSTFORGE_EMAIL` | E-mail do operador. Conta única. |
| `GHOSTFORGE_PASSWORD_HASH` | Hash gerado pelo comando a seguir. |
| `GHOSTFORGE_SESSION_SECRET` | Valor aleatório, exclusivo para assinar a sessão. |
| `GHOSTFORGE_API_TOKEN` | Valor aleatório, usado pelos clientes HTTP e MCP. |
| `GHOSTFORGE_RUNNER_TOKEN` | Valor aleatório diferente, usado apenas nos endpoints `/runner/`. |
| `GITHUB_RENDER_REPO` | `owner/repo` público com o workflow. Use o mesmo repositório da implantação. |
| `GITHUB_RENDER_TOKEN` | Fine-grained PAT apenas do repo de render, com permissão **Actions: write**. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | OAuth do Drive na conta de armazenamento. |
| `GOOGLE_DRIVE_FOLDER_ID` | ID de uma pasta criada com a mesma conta e cliente OAuth. |

Gere o hash com `python cloud_auth.py` depois de instalar `requirements-local.txt`. A senha é solicitada sem eco. Guarde a senha no gerenciador do operador. Gere os três segredos separadamente com `python -c "import secrets; print(secrets.token_urlsafe(48))"`. No Coolify, configure um novo hash para trocar a senha. Troque também `GHOSTFORGE_SESSION_SECRET` para revogar sessões existentes.

### Drive

No Google Cloud, habilite Drive API, configure o consentimento e crie um cliente OAuth do tipo Desktop. Autorize na sua máquina com o escopo `https://www.googleapis.com/auth/drive.file`, em modo offline, com a conta que deve possuir os arquivos. Com esse mesmo cliente, crie a pasta pela API e copie o ID para o Coolify. Para instalação duradoura, confira o estado da tela de consentimento e a validade do refresh token.

`drive_setup.py` faz a autorização e cria a pasta. Rode em uma máquina com navegador, instale `google-auth-oauthlib` e passe o JSON do cliente e um arquivo privado de saída que ainda não exista.

```sh
python -m pip install google-auth-oauthlib
python drive_setup.py /caminho/cliente-oauth.json /caminho/privado/ghostforge-drive.env
```

O processo guarda os vídeos selecionados e templates no Drive quando prepara o lote, reutilizando arquivos de mesmo SHA-256. A entrada do editor e os downloads do GhostReel ficam como cache de trabalho no volume. A saída vai do runner direto ao Drive via sessão de upload, sem artefato, release ou commit com a mídia. Faça backup do volume, especialmente da fila SQLite. Monitore o espaço livre e ajuste o limite de upload do proxy HTTPS à sua operação. O lote aceita até 40 vídeos e 3 GiB totais.

### API e MCP

Use `Authorization: Bearer <GHOSTFORGE_API_TOKEN>` nos clientes.

| Rota | Operação |
| --- | --- |
| `GET /api/state` | Lista a entrada do editor. |
| `POST /api/drive/import` | Importa o arquivo autorizado. Campos `file_id` e `filename`. |
| `POST /api/jobs` | Cria o lote. Exemplo `{"videos":[{"filename":"teste.mp4"}],"global_quality":"720p"}`. |
| `GET /api/jobs` | Lista lotes recentes. |
| `GET /api/jobs/{batch_id}` | Retorna o estado por vídeo e seu `output_url`. |
| `GET /api/render-output/{job_id}` | Transmite a saída autenticada do Drive. |

Para MCP stdio, registre um servidor `ghostforge` com o Python do ambiente virtual e o caminho absoluto de `cloud_mcp.py`. Injete `GHOSTFORGE_PUBLIC_URL` e `GHOSTFORGE_API_TOKEN` no ambiente do cliente MCP. As ferramentas são `list_inputs`, `import_from_drive`, `submit_render`, `list_batches` e `render_status`.

Um agente pode chamar `list_inputs`, enviar `submit_render` com os itens de `batch.videos` e consultar o `batch_id` em `render_status`. Cada resultado concluído inclui `output_url` para download pela API autenticada. O download dos originais pelos runners passa pela VPS em streaming, com a autorização do Drive mantida no servidor.

### Operação

- O workflow distribui um vídeo por job e permite até oito jobs em paralelo. Cada lote é uma execução separada.
- Um erro explícito de envio aparece no lote. Um timeout de dispatch pode significar que o GitHub recebeu a chamada. A fila aguarda os runners para evitar duplicar a execução. Jobs sem resposta expiram após três horas. Envie novamente apenas os vídeos que falharam.
- Em **Explorar Reels**, configure a integração oficial abaixo para contas profissionais. O botão de conexão por cookie atende à integração legada, cuja disponibilidade depende dos endpoints do Instagram.

### Instagram Business Discovery

Em um app Meta com Instagram API com Facebook Login, vincule uma conta profissional do Instagram a uma Página do Facebook. Gere um token de usuário autorizado com `instagram_basic`, `instagram_manage_insights` e `pages_read_engagement`; consulte a documentação para as permissões adicionais da sua configuração. Configure `REELS_GRAPH_TOKEN`, `REELS_GRAPH_ACCOUNT_ID` (ID da conta profissional que faz a consulta) e, opcionalmente, `REELS_GRAPH_VERSION` (padrão `v27.0`). Use as variáveis do processo local ou o painel do Coolify e reinicie o serviço.

A consulta oficial cobre contas profissionais elegíveis. Métricas omitidas aparecem como indisponíveis. O download e o envio ao editor ficam disponíveis quando a Meta retorna a URL do vídeo. A Meta pode omitir a URL quando o criador desativa downloads ou quando o áudio tem restrições. [Documentação Business Discovery](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery)

### Local

```sh
python -m venv .venv
python -m pip install -r requirements-local.txt
python local.py
```

O comando usa a porta local 8782. Para rede pública, use o Compose com HTTPS e login obrigatório.

[Matrizes Actions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations) · [API Actions](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event) · [Escopos do Drive](https://developers.google.com/workspace/drive/api/guides/api-specific-auth) · [Compose no Coolify](https://coolify.io/docs/applications/builds/docker-compose)
