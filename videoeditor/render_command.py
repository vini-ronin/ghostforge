"""Composição 9:16 para os runners Linux."""
from pathlib import Path


def build(source: Path, template: Path | None, output: Path, config: dict, job: dict) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source)]
    if template:
        cmd.extend(["-loop", "1", "-i", str(template)])
    top, bottom, left, right = [config.get(f"crop_{side}", 0) / 100 for side in ("t", "b", "l", "r")]
    filters = []
    if any((top, bottom, left, right)):
        filters.append(f"crop=trunc(iw*(1-{left}-{right})/2)*2:trunc(ih*(1-{top}-{bottom})/2)*2:iw*{left}:ih*{top}")
    if config.get("flip"):
        filters.append("hflip")
    if config.get("no_border") or job.get("global_no_border"):
        filters += ["scale=1080:1920:force_original_aspect_ratio=increase", "crop=1080:1920"]
    else:
        filters.append("scale=1080:-2")
    factor = config.get("scale", 1)
    filters.append(rf"scale=max(2\,trunc(iw*{factor}/2)*2):max(2\,trunc(ih*{factor}/2)*2)")
    filters.append("setsar=1")
    x, y = config.get("x_offset", 0), config.get("y_offset", 0)
    color = config.get("bg_color", "#000000").replace("#", "0x")
    graph = f"[0:v]{','.join(filters)}[vid];color=c={color}:s=1080x1920[bg];"
    overlay = f"overlay=(W-w)/2+({x}):(H-h)/2+({y}):shortest=1"
    if not template:
        graph += f"[bg][vid]{overlay}[composed];"
    elif config.get("comp_mode") == "crop_template":
        graph += f"[1:v]scale=1080:1920[tpl];[bg][vid]{overlay}[base];[base][tpl]overlay=shortest=1[composed];"
    else:
        graph += f"[1:v]scale=1080:1920[tpl];[bg][tpl]overlay=shortest=1[base];[base][vid]{overlay}[composed];"
    if job.get("global_quality") == "720p":
        graph += "[composed]scale=720:1280[scaled];[scaled]format=yuv420p[out]"
    else:
        graph += "[composed]format=yuv420p[out]"
    cmd.extend(["-filter_complex_threads", "2", "-filter_complex", graph, "-map", "[out]", "-map", "0:a?"])
    if job.get("global_audio_enhance"):
        cmd.extend(["-af", "highpass=f=80"])
    return cmd + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-threads", "3",
                  "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", str(output)]
