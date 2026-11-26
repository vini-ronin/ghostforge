import os
import json
import asyncio
import subprocess
import shutil
import re
from pathlib import Path
from uuid import uuid4
import cv2
import numpy as np
from typing import List, Dict, Any
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

app = FastAPI(title="GhostForge")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.environ.get("REELS_EDITOR_INPUT", os.path.join(BASE_DIR, "input"))
OUTPUT_DIR = os.environ.get("REELS_EDITOR_OUTPUT", os.path.join(BASE_DIR, "output"))
STATIC_DIR = os.path.join(BASE_DIR, "static")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def video_path(filename: str | None) -> Path:
    base = Path(INPUT_DIR).resolve()
    if not filename or not re.fullmatch(r"[\w .()-]{1,200}", filename):
        raise HTTPException(422, "Nome de vídeo inválido.")
    path = (base / filename).resolve()
    if path.parent != base or path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(422, "Formato de vídeo inválido.")
    return path

os.makedirs(INPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/input", StaticFiles(directory=INPUT_DIR), name="input")
app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

class VideoItemConfig(BaseModel):
    filename: str
    x_offset: int = 0
    y_offset: int = 0  # Padronizado para 0 (centro)
    scale: float = 1.0
    crop_t: int = 0
    crop_b: int = 0
    crop_l: int = 0
    crop_r: int = 0
    comp_mode: str = "overlay"
    bg_color: str = "#000000"
    no_border: bool = False
    flip: bool = False
    delete: bool = False

class BatchJob(BaseModel):
    global_anti_hash: bool = True
    global_quality: str = "1080p"
    global_audio_enhance: bool = True
    global_no_border: bool = False
    use_amd_gpu: bool = True  # GPU SEMPRE ATIVADA POR PADRAO
    videos: List[VideoItemConfig]

class AutoCropRequest(BaseModel):
    filenames: List[str]

CURRENT_JOB: Dict[str, Any] = {}

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html", context={
            "explorer_path": getattr(app.state, "explorer_path", None),
            "production_mode": getattr(app.state, "production_mode", False),
        },
    )

@app.get("/api/state")
async def get_state():
    files = [f for f in os.listdir(INPUT_DIR) if f.lower().endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm'))]
    template_exists = os.path.exists(os.path.join(INPUT_DIR, "template.png"))
    return {
        "videos": files,
        "sources": {name: read_source(name) for name in files if name.startswith("instagram-")},
        "template": "/input/template.png" if template_exists else None,
        "total_videos": len(files)
    }


def read_source(filename: str) -> dict:
    try:
        with open(os.path.join(INPUT_DIR, f"{filename}.json"), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

from PIL import Image, ImageDraw, ImageFont

class CaptionRequest(BaseModel):
    filename: str
    text: str
    y_pos: int = 480

@app.post("/api/generate_caption")
async def generate_caption(req: CaptionRequest):
    out_name = f"caption_{video_path(req.filename).name}.png"
    out_path = os.path.join(INPUT_DIR, out_name)
    
    # Carrega o template base se existir, senao cria transparente
    base_template = os.path.join(INPUT_DIR, "template.png")
    if os.path.exists(base_template):
        img = Image.open(base_template).convert("RGBA")
        img = img.resize((1080, 1920))
    else:
        img = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
        
    draw = ImageDraw.Draw(img)
    
    font_path = "C:\\Windows\\Fonts\\arialbd.ttf" # Negrito destaca mais
    if not os.path.exists(font_path):
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        
    try:
        font = ImageFont.truetype(font_path, 55)
    except:
        font = ImageFont.load_default()

    # Wrap text
    max_width = 900
    words = req.text.split()
    lines = []
    current_line = []
    
    for word in words:
        current_line.append(word)
        line_w = draw.textlength(" ".join(current_line), font=font)
        if line_w > max_width:
            current_line.pop()
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [word]
    if current_line:
        lines.append(" ".join(current_line))
        
    # Posição do texto baseada na requisição
    y_text = req.y_pos 
    for line in lines:
        line_w = draw.textlength(line, font=font)
        x_text = (1080 - line_w) / 2  # Centralizado
        draw.text((x_text, y_text), line, font=font, fill=(0, 0, 0, 255))
        y_text += 70
        
    img.save(out_path)
    
    return {
        "status": "ok", 
        "url": f"/input/{out_name}?v={os.path.getmtime(out_path)}",
        "box_height": y_text
    }

@app.post("/api/upload_template")
async def upload_template(file: UploadFile = File(...)):
    dest = os.path.join(INPUT_DIR, "template.png")
    try:
        with Image.open(file.file) as image:
            if image.width * image.height > 20_000_000:
                raise HTTPException(413, "Template muito grande.")
            image.convert("RGBA").save(dest, format="PNG")
    except (OSError, Image.DecompressionBombError):
        raise HTTPException(422, "Envie um PNG ou JPEG válido.") from None
        
    return {"status": "ok", "url": "/input/template.png"}

@app.post("/api/upload_videos")
def upload_videos(files: List[UploadFile] = File(...)):
    selected = [(item, video_path(item.filename)) for item in files]
    saved = []
    for file, destination in selected:
        temp = destination.with_name("." + uuid4().hex + ".part")
        try:
            with temp.open("xb") as buffer:
                shutil.copyfileobj(file.file, buffer, 1024 * 1024)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
        saved.append(destination.name)
    return {"status": "ok", "count": len(saved), "files": saved}

@app.api_route("/api/open_folder", methods=["GET", "POST"])
async def open_folder(folder_type: str = "output"):
    target = OUTPUT_DIR if folder_type == "output" else INPUT_DIR
    target = os.path.abspath(target)
    os.makedirs(target, exist_ok=True)
    if os.name == 'nt':
        try:
            os.startfile(target)
        except Exception:
            import subprocess
            subprocess.Popen(['explorer', target])
    return {"status": "ok", "target": target}

@app.post("/api/delete_video")
async def delete_video(filename: str):
    filepath = video_path(filename)
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
        except Exception as e:
            return {"status": "error", "message": str(e)}
    caption_path = os.path.join(INPUT_DIR, f"caption_{filename}.png")
    if os.path.exists(caption_path):
        try:
            os.remove(caption_path)
        except:
            pass
    return {"status": "ok", "filename": filename}

@app.post("/api/clear_all_videos")
async def clear_all_videos():
    deleted = 0
    for f in os.listdir(INPUT_DIR):
        if f.lower().endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm')) or f.startswith('caption_'):
            try:
                os.remove(os.path.join(INPUT_DIR, f))
                deleted += 1
            except:
                pass
    return {"status": "ok", "deleted": deleted}

@app.post("/api/autocrop")
async def autocrop(req: AutoCropRequest):
    results = {}
    for filename in req.filenames:
        filepath = str(video_path(filename))
        if not os.path.exists(filepath): continue
        
        try:
            cap = cv2.VideoCapture(filepath)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            frames_to_check = [total_frames // 4, total_frames // 2, total_frames * 3 // 4]
            
            best_rect = None
            
            for f_idx in frames_to_check:
                cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, f_idx))
                ret, frame = cap.read()
                if not ret: continue
                
                H, W = frame.shape[:2]
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                
                _, thresh_b = cv2.threshold(gray, 20, 255, cv2.THRESH_BINARY)
                _, thresh_w = cv2.threshold(gray, 235, 255, cv2.THRESH_BINARY_INV)
                mask = cv2.bitwise_and(thresh_b, thresh_w)
                
                kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 40))
                mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
                
                contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if contours:
                    largest_contour = max(contours, key=cv2.contourArea)
                    x, y, w, h = cv2.boundingRect(largest_contour)
                    if best_rect is None:
                        best_rect = [x, y, x+w, y+h]
                    else:
                        best_rect[0] = min(best_rect[0], x)
                        best_rect[1] = min(best_rect[1], y)
                        best_rect[2] = max(best_rect[2], x+w)
                        best_rect[3] = max(best_rect[3], y+h)

            cap.release()
            
            if best_rect:
                bx, by, bx2, by2 = best_rect
                crop_t = int((by / H) * 100)
                crop_b = int(((H - by2) / H) * 100)
                crop_l = int((bx / W) * 100)
                crop_r = int(((W - bx2) / W) * 100)
                
                results[filename] = {
                    "crop_t": max(0, min(50, crop_t)),
                    "crop_b": max(0, min(50, crop_b)),
                    "crop_l": max(0, min(50, crop_l)),
                    "crop_r": max(0, min(50, crop_r))
                }
        except Exception as e:
            print(f"Erro AutoCrop {filename}: {e}")
            
    return results

@app.post("/api/setup_job")
async def setup_job(job: BatchJob):
    global CURRENT_JOB
    CURRENT_JOB = job.model_dump()
    return {"status": "ok"}

async def render_video(config: dict, job_config: dict):
    filename = config['filename']
    filepath = os.path.join(INPUT_DIR, filename)
    out_path = os.path.join(OUTPUT_DIR, f"forged_{filename}")
    
    template_url = config.get('template_path')
    if template_url:
        import urllib.parse
        parsed = urllib.parse.urlparse(template_url)
        tpl_filename = os.path.basename(parsed.path)
        template_path = os.path.join(INPUT_DIR, tpl_filename)
    else:
        template_path = os.path.join(INPUT_DIR, "template.png")
        
    has_template = os.path.exists(template_path)

    yield f"data: [INFO] Renderizando: {filename}\n\n"

    cmd = ["ffmpeg", "-nostdin", "-y", "-i", filepath]
    if has_template:
        # IMPORTANTE: -loop 1 para a imagem durar o vídeo todo, senao o FFmpeg congela em 1 frame!
        cmd.extend(["-loop", "1", "-i", template_path])

    v_chains = []
    
    if job_config.get('global_anti_hash'):
        v_chains.append("normalize")
    if config.get('flip'):
        v_chains.append("hflip")

    ct = config.get('crop_t', 0) / 100.0
    cb = config.get('crop_b', 0) / 100.0
    cl = config.get('crop_l', 0) / 100.0
    cr = config.get('crop_r', 0) / 100.0
    
    if ct > 0 or cb > 0 or cl > 0 or cr > 0:
        crop_w = f"trunc(iw*(1-{cl}-{cr})/2)*2"
        crop_h = f"trunc(ih*(1-{ct}-{cb})/2)*2"
        crop_x = f"trunc(iw*{cl}/2)*2"
        crop_y = f"trunc(ih*{ct}/2)*2"
        v_chains.append(f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}")

    if config.get('no_border') or job_config.get('global_no_border'):
        v_chains.append("scale=1080:1920:force_original_aspect_ratio=increase")
        v_chains.append("crop=1080:1920")
    else:
        v_chains.append("scale=1080:-2:force_original_aspect_ratio=decrease")

    scale = config.get('scale', 1.0)
    if scale != 1.0:
        v_chains.append(f"scale=trunc(iw*{scale}/2)*2:trunc(ih*{scale}/2)*2")

    if job_config.get('global_anti_hash'):
        v_chains.append("setpts=PTS/1.05")
        
    v_pre = ",".join(v_chains) if v_chains else "null"

    x_off = config.get('x_offset', 0)
    y_off = config.get('y_offset', 0)
    comp_mode = config.get('comp_mode', 'overlay')
    bg_color = config.get('bg_color', '#000000').replace('#', '0x')

    # IMPORTANTE: shortest=1 em todos os overlays para evitar loop infinito do color=c
    filter_complex = f"[0:v]{v_pre}[vid];color=c={bg_color}:s=1080x1920[bg];"

    if has_template:
        filter_complex += f"[1:v]scale=1080:1920[tpl];"
        if comp_mode == 'crop_template':
            # Template on top: BG -> Video -> Template
            filter_complex += f"[bg][vid]overlay=(W-w)/2+{x_off}:(H-h)/2+{y_off}:shortest=1[vid_on_bg];"
            filter_complex += f"[vid_on_bg][tpl]overlay=0:0:shortest=1[composed];"
        else:
            # Video on top: BG -> Template -> Video
            filter_complex += f"[bg][tpl]overlay=0:0:shortest=1[bg_tpl];"
            filter_complex += f"[bg_tpl][vid]overlay=(W-w)/2+{x_off}:(H-h)/2+{y_off}:shortest=1[composed];"
    else:
        filter_complex += f"[bg][vid]overlay=(W-w)/2+{x_off}:(H-h)/2+{y_off}:shortest=1[composed];"

    if job_config.get('global_anti_hash'):
        filter_complex += f"[composed]scale='trunc(iw*1.026/2)*2':'trunc(ih*1.026/2)*2',crop=1080:1920:(iw-1080)/2:(ih-1920)/2,format=yuv420p[out]"
    else:
        filter_complex += f"[composed]format=yuv420p[out]"

    cmd.extend(["-filter_complex", filter_complex, "-map", "[out]"])

    cmd.extend(["-map", "0:a?"])
    a_filters = []
    if job_config.get('global_audio_enhance') or job_config.get('global_anti_hash'):
        a_filters.append("highpass=f=80")
    if job_config.get('global_anti_hash'):
        a_filters.append("atempo=1.05")
        
    if a_filters:
        cmd.extend(["-af", ",".join(a_filters)])

    if job_config.get('global_anti_hash'):
        cmd.extend(["-map_metadata", "-1"])

    use_gpu = job_config.get('use_amd_gpu', True)
    if use_gpu:
        cmd.extend(["-c:v", "h264_amf", "-quality", "speed", "-rc", "cbr", "-b:v", "6M", "-c:a", "aac", "-b:a", "192k", out_path])
    else:
        cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac", "-b:a", "192k", out_path])

    yield f"data: [GPU AMF: {'SIM' if use_gpu else 'NAO'}] Iniciando encode de {filename}...\n\n"

    process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = await process.communicate()

    if process.returncode == 0:
        yield f"data: [SUCESSO] {filename} -> Exportado 1080x1920! (Pasta output/)\n\n"
    else:
        print(f"FFMPEG GPU ERROR: {stderr.decode()}")
        if use_gpu:
            yield f"data: [AVISO] Falha GPU AMF. Fallback para CPU libx264...\n\n"
            cmd_cpu = [c if c != "h264_amf" else "libx264" for c in cmd]
            p2 = await asyncio.create_subprocess_exec(*cmd_cpu, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout2, stderr2 = await p2.communicate()
            if p2.returncode == 0:
                yield f"data: [SUCESSO] {filename} -> Concluido via CPU 1080x1920!\n\n"
                return
        yield f"data: [ERRO] Falha ao renderizar {filename}.\n\n"

@app.post("/api/render_single")
async def render_single_endpoint(payload: dict):
    config = payload.get("config", {})
    job_config = payload.get("job_config", {})
    filename = config.get("filename")
    if not filename:
        return {"status": "error", "message": "Filename not provided"}
    
    out_name = f"forged_{filename}"
    out_path = os.path.join(OUTPUT_DIR, out_name)
    
    if os.path.exists(out_path):
        os.remove(out_path)
        
    # Consome o generator para rodar a renderização
    async for _ in render_video(config, job_config):
        pass
        
    if os.path.exists(out_path):
        return {"status": "ok", "url": f"/output/{out_name}", "filename": out_name}
    return {"status": "error", "message": "Failed to render video"}

@app.get("/api/process_stream")
async def process_stream():
    async def event_generator():
        global CURRENT_JOB
        if not CURRENT_JOB or not CURRENT_JOB.get('videos'):
            yield "data: [ERRO] Nenhum lote configurado.\n\n"
            yield "data: [DONE]\n\n"
            return

        videos = [v for v in CURRENT_JOB['videos'] if not v.get('delete')]
        yield f"data: [SISTEMA] GhostForge Engine Init. Total na fila: {len(videos)} videos.\n\n"

        for v in videos:
            async for log in render_video(v, CURRENT_JOB):
                yield log

        yield "data: [SISTEMA] Processamento em massa finalizado com sucesso!\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
