import os
import json
import zipfile
import asyncio
import re
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit
import requests
from fastapi import FastAPI, Request, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

app = FastAPI(title='GhostReel Explorer')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount('/static', StaticFiles(directory=os.path.join(BASE_DIR, 'static')), name='static')
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, 'templates'))

DOWNLOAD_DIR = os.environ.get('REELS_DOWNLOAD_DIR', os.path.join(BASE_DIR, 'downloads'))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

SESSION_FILE = os.environ.get('REELS_SESSION_FILE', os.path.join(BASE_DIR, 'session.json'))
GRAPH_TOKEN = os.environ.get('REELS_GRAPH_TOKEN', '').strip()
GRAPH_ACCOUNT = os.environ.get('REELS_GRAPH_ACCOUNT_ID', '').strip()
GRAPH_VERSION = os.environ.get('REELS_GRAPH_VERSION', '').strip() or 'v27.0'
EDITOR_INPUT = Path(os.environ.get('REELS_EDITOR_INPUT', Path(BASE_DIR).parent / 'videoeditor' / 'input')).resolve()
EDITOR_URL = os.environ.get('REELS_EDITOR_URL', 'http://127.0.0.1:8782')

def load_saved_session():
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_session_data(sid: str, username: str = None, user_id: str = None):
    data = {'sessionid': sid, 'username': username, 'user_id': user_id}
    with open(SESSION_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    return data

def get_direct_video_url(shortcode: str, cached_vurl: str = None) -> str:
    # 1. Se URL do CDN foi fornecida e ainda responde OK
    if valid_cdn(cached_vurl):
        try:
            r = requests.head(cached_vurl, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5, allow_redirects=False)
            if r.status_code == 200:
                return cached_vurl
        except Exception:
            pass

    if GRAPH_TOKEN or GRAPH_ACCOUNT:
        return None
    # 2. Busca URL atualizada via API Mobile
    try:
        sess = load_saved_session()
        headers = {
            'User-Agent': 'Instagram 319.0.0.38.109 Android',
            'X-IG-App-ID': '936619743392459',
        }
        cookies = {'sessionid': sess.get('sessionid'), 'ds_user_id': sess.get('user_id')} if sess.get('sessionid') else {}
        r_info = requests.get(f'https://i.instagram.com/api/v1/media/{shortcode}/info/', headers=headers, cookies=cookies, timeout=8)
        if r_info.status_code == 200:
            items = r_info.json().get('items', [])
            if items and items[0].get('video_versions'):
                return items[0]['video_versions'][0].get('url')
    except Exception:
        pass
    return None


def valid_cdn(url: str | None) -> bool:
    try:
        uri = urlsplit(url or "")
        host = uri.hostname or ""
        return (
            uri.scheme == "https" and uri.port in (None, 443) and not uri.username
            and host.endswith((".cdninstagram.com", ".fbcdn.net"))
        )
    except ValueError:
        return False


def cached_video(shortcode: str, cached_url: str | None) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", shortcode):
        return None
    target = Path(DOWNLOAD_DIR) / f"{shortcode}.mp4"
    if target.is_file() and target.stat().st_size > 1000:
        return target
    url = get_direct_video_url(shortcode, cached_url)
    if not valid_cdn(url):
        return None
    with requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, stream=True,
                      timeout=(8, 90), allow_redirects=False) as response:
        if response.status_code != 200:
            return None
        with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR, prefix=".asset-") as folder:
            temp = Path(folder) / "video"
            with temp.open("wb") as handle:
                size = 0
                for block in response.iter_content(1024 * 1024):
                    size += len(block)
                    if size > 1024**3:
                        return None
                    handle.write(block)
            if size <= 1000:
                return None
            os.replace(temp, target)
    return target

class DownloadItem(BaseModel):
    shortcode: str
    video_url: str = None
    url: str = None

class DownloadRequest(BaseModel):
    items: list[DownloadItem]

class SessionRequest(BaseModel):
    sessionid: str


class EditorImport(BaseModel):
    caption: str = Field(default='', max_length=3000)
    username: str = Field(default='', max_length=100)

@app.get('/', response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(
        request=request, name='index.html', context={'editor_path': getattr(app.state, 'editor_path', None)},
    )

@app.get('/api/proxy_image')
def proxy_image(url: str):
    if not valid_cdn(url):
        return Response(status_code=404)
    try:
        with requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, stream=True,
                          timeout=8, allow_redirects=False) as response:
            mime = response.headers.get("Content-Type", "")
            if response.status_code != 200 or mime.split(";")[0] not in {"image/jpeg", "image/png", "image/webp", "image/avif"}:
                return Response(status_code=404)
            data = bytearray()
            for block in response.iter_content(65536):
                data.extend(block)
                if len(data) > 8 * 1024**2:
                    return Response(status_code=413)
            return Response(bytes(data), media_type=mime)
    except requests.RequestException:
        return Response(status_code=404)

@app.get('/api/session/status')
def get_session_status():
    if GRAPH_TOKEN or GRAPH_ACCOUNT:
        return {'logged_in': False, 'provider': 'meta'}
    sess = load_saved_session()
    if sess.get('sessionid'):
        username = sess.get('username')
        verified = bool(username and username != 'conectado')
        return {'logged_in': verified, 'session_saved': True, 'username': username if verified else None}
    return {'logged_in': False}


def instagram_error(response, operation):
    code = response.status_code
    try:
        data = response.json()
    except ValueError:
        data = None
    message = data.get('message') if isinstance(data, dict) else None
    if message in ('challenge_required', 'checkpoint_required'):
        return f'O Instagram pediu uma verificação da conta. Conclua no Instagram e reconecte a sessão. (HTTP {code})'
    if code == 429:
        return 'O Instagram limitou as consultas. Aguarde e tente novamente. (HTTP 429)'
    if code == 401 or message == 'login_required':
        return f'O Instagram pediu login. Reconecte a sessão no GhostReel. (HTTP {code})'
    if code == 403:
        return 'O Instagram recusou o acesso para esta sessão. Confira sua conta no Instagram. (HTTP 403)'
    return f'O Instagram não conseguiu {operation}. (HTTP {code})'


@app.post('/api/session')
async def save_session(req: SessionRequest):
    try:
        sid = req.sessionid.strip()
        user_id = sid.split('%3A')[0] if '%3A' in sid else sid.split(':')[0]
        if not re.fullmatch(r'[0-9]+', user_id) or sid == user_id:
            return JSONResponse(status_code=400, content={'error': 'Informe um cookie sessionid válido do Instagram.'})
        
        headers = {
            'User-Agent': 'Instagram 319.0.0.38.109 Android (33/13; 420dpi; 1080x2400; samsung; SM-G998B; p3s; exynos2100; en_US)',
            'X-IG-App-ID': '936619743392459',
        }
        cookies = {'sessionid': sid, 'ds_user_id': user_id}
        r = requests.get(f'https://i.instagram.com/api/v1/users/{user_id}/info/', headers=headers, cookies=cookies, timeout=10)
        
        if r.status_code != 200:
            return JSONResponse(status_code=502, content={'error': instagram_error(r, 'confirmar a conta')})
        username = (r.json().get('user') or {}).get('username')
        if not username:
            return JSONResponse(status_code=502, content={'error': 'O Instagram não confirmou a conta nesta sessão.'})
            
        save_session_data(sid, username, user_id)
        return {'status': 'success', 'username': username, 'message': f'Conta @{username} conectada com sucesso!'}
    except (requests.RequestException, ValueError):
        return JSONResponse(status_code=502, content={'error': 'Não foi possível confirmar a sessão com o Instagram.'})

def graph_profile_events(username: str, limit: int, media_type: str):
    def event(kind, **values):
        return 'data: ' + json.dumps({'type': kind, **values}) + '\n\n'

    count = 0
    cursor = None
    seen = set()
    try:
        if not re.fullmatch(r'[A-Za-z0-9._]{1,30}', username):
            raise ValueError('Informe um nome de usuário válido do Instagram.')
        if not GRAPH_TOKEN or not re.fullmatch(r'[0-9]+', GRAPH_ACCOUNT) or not re.fullmatch(r'v[0-9]+\.[0-9]+', GRAPH_VERSION):
            raise ValueError('Configure o ID da conta, o token e a versão da API Meta no GhostReel.')
        with requests.Session() as session:
            session.headers['Authorization'] = f'Bearer {GRAPH_TOKEN}'
            while count < limit:
                edge = f'media.limit({min(limit - count, 50)})'
                if cursor:
                    edge += f'.after({cursor})'
                fields = (f'business_discovery.username({username}){{username,name,biography,followers_count,follows_count,'
                          f'media_count,profile_picture_url,{edge}{{caption,media_type,media_product_type,media_url,'
                          'thumbnail_url,permalink,timestamp,view_count,like_count,comments_count}}')
                response = session.get(f'https://graph.facebook.com/{GRAPH_VERSION}/{GRAPH_ACCOUNT}',
                                       params={'fields': fields}, timeout=15, allow_redirects=False)
                if not response.ok:
                    error = response.json().get('error', {})
                    code = error.get('code')
                    if code == 190:
                        raise ValueError('A Meta recusou o token da integração. Atualize REELS_GRAPH_TOKEN.')
                    raise ValueError(f'A Meta não disponibilizou @{username} ao Business Discovery. '
                                     f'Confira o tipo de conta e as permissões. (HTTP {response.status_code})')
                profile = response.json().get('business_discovery')
                if not profile:
                    raise ValueError(f'A Meta não retornou dados profissionais para @{username}.')
                if cursor is None:
                    yield event('profile', profile={
                        'username': profile.get('username', username), 'full_name': profile.get('name', ''),
                        'biography': profile.get('biography', ''), 'followers': profile.get('followers_count', 0),
                        'following': profile.get('follows_count', 0), 'total_posts': profile.get('media_count', 0),
                        'profile_pic_url': profile.get('profile_picture_url', ''), 'related_profiles': [],
                    })
                media = profile.get('media') or {}
                batch = []
                for item in media.get('data', []):
                    is_video = item.get('media_type') == 'VIDEO'
                    is_sidecar = item.get('media_type') == 'CAROUSEL_ALBUM'
                    if media_type == 'reels' and item.get('media_product_type') != 'REELS':
                        continue
                    if media_type == 'carousels' and not is_sidecar:
                        continue
                    url = item.get('permalink') or ''
                    match = re.fullmatch(r'https://(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)/?(?:\?.*)?', url)
                    if not match:
                        continue
                    media_url = item.get('media_url')
                    video_url = media_url if is_video and valid_cdn(media_url) else None
                    timestamp = item.get('timestamp')
                    batch.append({
                        'shortcode': match[1], 'username': username, 'url': url, 'video_url': video_url,
                        'downloadable': bool(video_url), 'thumbnail_url': item.get('thumbnail_url') or ('' if is_video else media_url) or '',
                        'views': item.get('view_count'), 'likes': item.get('like_count'), 'comments': item.get('comments_count'),
                        'duration': None, 'caption': (item.get('caption') or '')[:120],
                        'timestamp': int(datetime.fromisoformat(timestamp).timestamp()) if timestamp else 0,
                        'is_video': is_video, 'is_sidecar': is_sidecar,
                        'type_label': 'REEL' if item.get('media_product_type') == 'REELS' else 'VIDEO' if is_video else 'CARROSSEL' if is_sidecar else 'FOTO',
                    })
                    if count + len(batch) == limit:
                        break
                if batch:
                    count += len(batch)
                    yield event('reels_batch', reels=batch)
                cursor = (media.get('paging') or {}).get('cursors', {}).get('after')
                if not media.get('data') or not cursor or cursor in seen or not re.fullmatch(r'[A-Za-z0-9_+=/-]+', cursor):
                    break
                seen.add(cursor)
        yield event('done', count=count)
    except requests.RequestException:
        yield event('error', error='A consulta à API Meta falhou. Tente novamente em instantes.')
    except ValueError as error:
        yield event('error', error=str(error) if not isinstance(error, json.JSONDecodeError) else 'A Meta devolveu uma resposta inválida.')


@app.get('/api/scrape/stream')
async def scrape_stream(username: str, limit: int = 12, media_type: str = 'reels'):
    username = username.strip().removeprefix('@')
    if GRAPH_TOKEN or GRAPH_ACCOUNT:
        return StreamingResponse(graph_profile_events(username, min(limit, 9999), media_type), media_type='text/event-stream')
    async def event_generator():
        try:
            if not re.fullmatch(r'[A-Za-z0-9._]{1,30}', username):
                raise ValueError('Informe um nome de usuário válido do Instagram.')
            sess = load_saved_session()
            sid = sess.get('sessionid')
            user_id_cookie = sess.get('user_id') or (sid.split('%3A')[0] if sid and '%3A' in sid else '')

            headers = {
                'User-Agent': 'Instagram 319.0.0.38.109 Android (33/13; 420dpi; 1080x2400; samsung; SM-G998B; p3s; exynos2100; en_US)',
                'X-IG-App-ID': '936619743392459',
            }
            cookies = {}
            if sid:
                cookies['sessionid'] = sid
                cookies['ds_user_id'] = user_id_cookie

            s = requests.Session()
            s.headers.update(headers)
            s.cookies.update(cookies)

            # 1. Obter perfil
            r_user = s.get(f'https://i.instagram.com/api/v1/users/web_profile_info/?username={username}', headers={'X-IG-App-ID': '936619743392459', 'User-Agent': 'Mozilla/5.0'}, timeout=8)
            
            target_user_id = None
            profile_data = {}
            
            if r_user.status_code == 200:
                u = (r_user.json().get('data') or {}).get('user') or {}
                target_user_id = u.get('id')
                if not target_user_id:
                    raise ValueError(f'O Instagram não retornou dados para @{username}.')
                
                related = []
                try:
                    r_chain = s.get(f'https://i.instagram.com/api/v1/discover/chaining/?target_id={target_user_id}', timeout=5)
                    if r_chain.status_code == 200:
                        for item in r_chain.json().get('users', [])[:20]:
                            if item.get('username'):
                                related.append({
                                    'username': item.get('username'),
                                    'full_name': item.get('full_name', ''),
                                    'profile_pic_url': item.get('profile_pic_url', '')
                                })
                except Exception as ex:
                    print(f"Error fetching chaining: {ex}")
                
                profile_data = {
                    'username': u.get('username', username),
                    'full_name': u.get('full_name', ''),
                    'biography': u.get('biography', ''),
                    'followers': u.get('edge_followed_by', {}).get('count', 0),
                    'following': u.get('edge_follow', {}).get('count', 0),
                    'profile_pic_url': u.get('profile_pic_url', ''),
                    'total_posts': u.get('edge_owner_to_timeline_media', {}).get('count', 0),
                    'related_profiles': related
                }
                if u.get('is_private') and u.get('followed_by_viewer') is False:
                    raise ValueError(f'@{username} é privado para esta sessão.')
            else:
                if r_user.status_code != 404:
                    raise ValueError(instagram_error(r_user, f'consultar @{username}'))
                r_lookup = s.get(f'https://i.instagram.com/api/v1/users/lookup/?q={username}', timeout=8)
                if r_lookup.status_code == 200:
                    u = r_lookup.json().get('user') or {}
                    if not u.get('pk'):
                        raise ValueError(f'O Instagram não retornou dados para @{username}.')
                    target_user_id = str(u.get('pk'))
                    profile_data = {
                        'username': u.get('username', username),
                        'full_name': u.get('full_name', ''),
                        'biography': '',
                        'followers': u.get('follower_count', 0),
                        'following': 0,
                        'profile_pic_url': u.get('profile_pic_url', ''),
                        'total_posts': 0,
                        'related_profiles': []
                    }
                else:
                    detail = instagram_error(r_lookup, f'consultar @{username}')
                    raise ValueError(f'{detail} A consulta inicial respondeu HTTP 404. '
                                     'Abra o perfil no Instagram ou configure a integração Meta para perfis profissionais.')

            yield f"data: {json.dumps({'type': 'profile', 'profile': profile_data})}\n\n"
            await asyncio.sleep(0.01)

            # 2. Extrair Posts / Reels via API Mobile Ultra-Rápida
            count = 0
            max_id = None
            has_more = True

            while count < limit and has_more:
                items = []
                
                if media_type == 'reels':
                    payload = {'target_user_id': str(target_user_id), 'page_size': str(min(limit - count, 20))}
                    if max_id:
                        payload['max_id'] = str(max_id)
                    r_clips = s.post('https://i.instagram.com/api/v1/clips/user/', data=payload, timeout=10)
                    if r_clips.status_code != 200:
                        raise ValueError(instagram_error(r_clips, 'listar os Reels'))
                    if r_clips.status_code == 200:
                        res = r_clips.json()
                        items = [c.get('media', {}) for c in res.get('items', [])]
                        max_id = res.get('paging_info', {}).get('max_id')
                        has_more = res.get('paging_info', {}).get('more_available', False)
                else:
                    url = f'https://i.instagram.com/api/v1/feed/user/{target_user_id}/'
                    if max_id:
                        url += f'?max_id={max_id}'
                    r_feed = s.get(url, timeout=10)
                    if r_feed.status_code != 200:
                        raise ValueError(instagram_error(r_feed, 'listar as publicações'))
                    if r_feed.status_code == 200:
                        res = r_feed.json()
                        items = res.get('items', [])
                        max_id = res.get('next_max_id')
                        has_more = res.get('more_available', False)

                if not items:
                    break

                batch = []
                for item in items:
                    if count >= limit:
                        break

                    is_video = (item.get('media_type') == 2) or bool(item.get('video_versions'))
                    is_sidecar = (item.get('media_type') == 8) or ('carousel_media' in item)

                    if media_type == 'reels' and not is_video:
                        continue
                    elif media_type == 'carousels' and not is_sidecar:
                        continue

                    shortcode = item.get('code')
                    views = item.get('play_count') or item.get('view_count') or 0
                    likes = item.get('like_count') or 0
                    comments = item.get('comment_count') or 0
                    duration = item.get('video_duration') or 0

                    caption_text = ''
                    if item.get('caption') and isinstance(item.get('caption'), dict):
                        caption_text = item['caption'].get('text', '')
                    if caption_text and len(caption_text) > 120:
                        caption_text = caption_text[:120] + '...'

                    thumb_url = ''
                    if item.get('image_versions2', {}).get('candidates'):
                        thumb_url = item['image_versions2']['candidates'][0].get('url', '')
                    
                    video_url = None
                    if item.get('video_versions') and len(item['video_versions']) > 0:
                        video_url = item['video_versions'][0].get('url')

                    type_label = 'REEL' if is_video else ('CARROSSEL' if is_sidecar else 'FOTO')

                    batch.append({
                        'shortcode': shortcode,
                        'username': (item.get('user') or {}).get('username') or username,
                        'url': f'https://instagram.com/reel/{shortcode}/' if is_video else f'https://instagram.com/p/{shortcode}/',
                        'video_url': video_url,
                        'thumbnail_url': thumb_url,
                        'views': views,
                        'likes': likes,
                        'comments': comments,
                        'duration': duration,
                        'caption': caption_text,
                        'timestamp': item.get('taken_at', 0),
                        'is_video': is_video,
                        'is_sidecar': is_sidecar,
                        'type_label': type_label
                    })
                    count += 1

                if batch:
                    yield f"data: {json.dumps({'type': 'reels_batch', 'reels': batch})}\n\n"
                    await asyncio.sleep(0.01)

            yield f"data: {json.dumps({'type': 'done', 'count': count})}\n\n"

        except Exception as e:
            err_raw = str(e)
            yield f"data: {json.dumps({'type': 'error', 'error': err_raw, 'raw_error': err_raw})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# Download individual direto para o navegador com salvamento local
@app.get('/api/download_video/{shortcode}')
def download_single_video(shortcode: str, vurl: str = None):
    try:
        target_path = cached_video(shortcode, vurl)
        if target_path:
            return FileResponse(target_path, filename=f"{shortcode}.mp4", media_type="video/mp4")
    except (requests.RequestException, OSError):
        return JSONResponse(status_code=502, content={'error': 'Não foi possível transferir o vídeo.'})
    return JSONResponse(status_code=404, content={'error': 'URL do vídeo indisponível.'})

# Download em lote empacotado em .ZIP direto para o navegador
@app.post('/api/download_zip')
def download_zip(req: DownloadRequest, background_tasks: BackgroundTasks):
    handle, filename = tempfile.mkstemp(suffix=".zip", dir=DOWNLOAD_DIR)
    os.close(handle)
    try:
        with zipfile.ZipFile(filename, 'w', zipfile.ZIP_STORED) as archive:
            for item in req.items:
                try:
                    media = cached_video(item.shortcode, item.video_url)
                    if media:
                        archive.write(media, arcname=media.name)
                except (requests.RequestException, OSError):
                    continue
    except Exception:
        Path(filename).unlink(missing_ok=True)
        raise
    background_tasks.add_task(Path(filename).unlink, missing_ok=True)
    return FileResponse(filename, media_type="application/zip", filename="ghostreel_batch.zip", background=background_tasks)

# Abrir pasta downloads no Windows Explorer
@app.post('/api/send_to_editor/{shortcode}')
def send_to_editor(shortcode: str, metadata: EditorImport, request: Request):
    origin = request.headers.get('origin')
    expected = os.environ.get("GHOSTFORGE_PUBLIC_URL") or str(request.base_url)
    if origin and origin != expected.rstrip('/'):
        return JSONResponse(status_code=403, content={'error': 'Origem inválida.'})
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', shortcode):
        return JSONResponse(status_code=422, content={'error': 'Identificador do Reel inválido.'})
    download_dir = Path(DOWNLOAD_DIR).resolve()
    source = (download_dir / f'{shortcode}.mp4').resolve()
    if not source.is_relative_to(download_dir) or not source.is_file() or source.stat().st_size <= 1000:
        return JSONResponse(status_code=404, content={'error': 'Baixe o vídeo no GhostReel antes de enviar ao editor.'})

    filename = f'instagram-{shortcode}.mp4'
    EDITOR_INPUT.mkdir(parents=True, exist_ok=True)
    target = EDITOR_INPUT / filename
    note = {
        'source': 'instagram',
        'url': f'https://www.instagram.com/reel/{shortcode}/',
        'username': metadata.username,
        'caption': metadata.caption,
    }
    try:
        with tempfile.TemporaryDirectory(dir=EDITOR_INPUT, prefix='.import-') as stage:
            stage = Path(stage)
            media = stage / filename
            sidecar = stage / f'{filename}.json'
            shutil.copyfile(source, media)
            sidecar.write_text(json.dumps(note, ensure_ascii=False), encoding='utf-8')
            os.replace(sidecar, EDITOR_INPUT / sidecar.name)
            os.replace(media, target)
    except OSError:
        return JSONResponse(status_code=500, content={'error': 'Não foi possível copiar o arquivo para o editor.'})
    editor_path = getattr(app.state, 'editor_path', None) or f'{EDITOR_URL.rstrip("/")}/'
    return {'filename': filename, 'editor_url': f'{editor_path}?video={filename}'}


@app.post('/api/open_folder')
def open_downloads_folder():
    try:
        os.startfile(DOWNLOAD_DIR)
        return {'status': 'success', 'message': 'Pasta aberta no Windows Explorer.'}
    except Exception as e:
        return JSONResponse(status_code=500, content={'error': str(e)})

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8000)
