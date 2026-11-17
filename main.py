"""
Zenith Copy — backend
Tempel link sosial media -> ambil info -> download video / gambar / audio.
Mesin: yt-dlp (video, 1000+ situs) + gallery-dl (gambar / carousel).
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
TMP = Path(tempfile.gettempdir()) / "zenith_copy"
TMP.mkdir(exist_ok=True)

SECRET = os.environ.get("ZENITH_SECRET", uuid.uuid4().hex).encode()
COOKIES = os.environ.get("COOKIES_FILE")  # opsional: cookies.txt (untuk IG/FB yang minta login)
MAX_ITEMS = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

VIDEO_EXT = {"mp4", "webm", "mov", "mkv", "m4v"}
IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif", "heic"}
AUDIO_EXT = {"mp3", "m4a", "aac", "ogg", "opus", "wav"}

app = FastAPI(title="Zenith Copy")


# ---------------------------------------------------------------- utils
def safe_url(u: str) -> str:
    """Hanya http/https ke alamat publik (cegah SSRF ke jaringan internal)."""
    u = u.strip()
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "Link tidak valid. Pastikan diawali http:// atau https://")
    try:
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise HTTPException(400, "Alamat tidak diizinkan.")
    except socket.gaierror:
        raise HTTPException(400, "Domain tidak ditemukan.")
    return u


def sign(s: str) -> str:
    return hmac.new(SECRET, s.encode(), hashlib.sha256).hexdigest()[:32]


def clean_name(s: str, fallback="zenith") -> str:
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", s or "").strip()
    return (s[:80] or fallback)


def platform_of(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    table = {
        "tiktok": "TikTok", "instagram": "Instagram", "youtu": "YouTube",
        "facebook": "Facebook", "fb.watch": "Facebook", "twitter": "X / Twitter",
        "x.com": "X / Twitter", "pinterest": "Pinterest", "pin.it": "Pinterest",
        "threads": "Threads", "reddit": "Reddit", "snapchat": "Snapchat",
        "likee": "Likee", "vimeo": "Vimeo", "twitch": "Twitch",
        "soundcloud": "SoundCloud", "bilibili": "Bilibili", "tumblr": "Tumblr",
        "dailymotion": "Dailymotion", "linkedin": "LinkedIn",
    }
    for k, v in table.items():
        if k in h:
            return v
    return h or "Web"


def ext_of(url: str) -> str:
    path = urlparse(url).path.lower()
    m = re.search(r"\.([a-z0-9]{2,4})$", path)
    return m.group(1) if m else ""


def ydl_base_opts() -> dict:
    o = {"quiet": True, "no_warnings": True, "http_headers": {"User-Agent": UA}}
    if COOKIES and Path(COOKIES).exists():
        o["cookiefile"] = COOKIES
    return o


# ------------------------------------------------------------ extractors
def info_ytdlp(url: str) -> dict | None:
    opts = ydl_base_opts() | {"skip_download": True, "extract_flat": False}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return None

    entries = info.get("entries")
    entries = list(entries)[:MAX_ITEMS] if entries else [info]
    items = []
    for idx, e in enumerate(entries, start=1):
        if not e:
            continue
        fmts = e.get("formats") or []
        has_video = any(f.get("vcodec") not in (None, "none") for f in fmts) or e.get("vcodec") not in (None, "none")
        has_audio = any(f.get("acodec") not in (None, "none") for f in fmts)
        direct_ext = (e.get("ext") or "").lower()

        options = []
        if direct_ext in IMAGE_EXT and not fmts:
            u = e.get("url")
            if u:
                options.append({"label": f"Gambar {direct_ext.upper()}", "kind": "file",
                                "src": u, "sig": sign(u), "ext": direct_ext})
        else:
            heights = sorted({f["height"] for f in fmts
                              if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
            if has_video:
                if heights:
                    for h in heights[:5]:
                        options.append({"label": f"Video {h}p", "kind": "ytdlp", "fmt": f"v{h}", "ext": "mp4"})
                else:
                    options.append({"label": "Video MP4", "kind": "ytdlp", "fmt": "vbest", "ext": "mp4"})
            if has_audio:
                options.append({"label": "Audio MP3", "kind": "ytdlp", "fmt": "audio", "ext": "mp3"})
            if not options:
                options.append({"label": "Download", "kind": "ytdlp", "fmt": "vbest", "ext": direct_ext or "mp4"})

        thumb = e.get("thumbnail") or info.get("thumbnail")
        if thumb:  # thumbnail / cover juga bisa diunduh sebagai gambar
            options.append({"label": "Thumbnail", "kind": "file", "src": thumb,
                            "sig": sign(thumb), "ext": ext_of(thumb) or "jpg"})

        items.append({
            "index": idx if info.get("entries") else 0,
            "title": e.get("title") or info.get("title"),
            "thumbnail": thumb,
            "duration": e.get("duration"),
            "options": options,
        })

    if not items:
        return None
    return {
        "title": info.get("title") or (items[0]["title"] if items else ""),
        "uploader": info.get("uploader") or info.get("channel") or "",
        "items": items,
    }


def info_gallerydl(url: str) -> dict | None:
    cmd = [sys.executable, "-m", "gallery_dl", "-j", url]
    if COOKIES and Path(COOKIES).exists():
        cmd[3:3] = ["--cookies", COOKIES]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None

    items, title, uploader = [], "", ""
    for msg in data:
        if not isinstance(msg, list) or not msg:
            continue
        if msg[0] == 3 and len(msg) >= 2:  # Message.Url
            u = msg[1]
            meta = msg[2] if len(msg) > 2 and isinstance(msg[2], dict) else {}
            ext = (meta.get("extension") or ext_of(u) or "jpg").lower()
            title = title or meta.get("description") or meta.get("content") or meta.get("title") or ""
            if not uploader:
                a = meta.get("author") or meta.get("user")
                uploader = (a.get("name") or a.get("username") if isinstance(a, dict) else a) \
                    or meta.get("username") or ""
            kind = "Video" if ext in VIDEO_EXT else "Audio" if ext in AUDIO_EXT else "Gambar"
            items.append({
                "index": len(items) + 1,
                "title": f"{kind} {len(items) + 1}",
                "thumbnail": u if ext in IMAGE_EXT else None,
                "duration": None,
                "options": [{"label": f"{kind} {ext.upper()}", "kind": "file",
                             "src": u, "sig": sign(u), "ext": ext}],
            })
            if len(items) >= MAX_ITEMS:
                break
    if not items:
        return None
    return {"title": str(title)[:150], "uploader": str(uploader), "items": items}


# ------------------------------------------------------------------ API
class InfoReq(BaseModel):
    url: str


@app.post("/api/info")
async def api_info(req: InfoReq):
    url = safe_url(req.url)
    errors = []
    for fn in (info_ytdlp, info_gallerydl):
        try:
            res = await asyncio.to_thread(fn, url)
            if res:
                res["platform"] = platform_of(url)
                res["source"] = url
                return res
        except HTTPException:
            raise
        except Exception as ex:  # noqa: BLE001
            errors.append(str(ex).split("\n")[0][:200])
    msg = "Media tidak ditemukan. Link mungkin private, butuh login, atau tidak didukung."
    raise HTTPException(422, {"message": msg, "detail": errors})


def _cleanup(path: Path):
    shutil.rmtree(path, ignore_errors=True)


@app.get("/api/download")
async def api_download(background: BackgroundTasks,
                       url: str = Query(...), fmt: str = "vbest", item: int = 0):
    url = safe_url(url)
    workdir = TMP / uuid.uuid4().hex
    workdir.mkdir()

    if fmt == "audio":
        fsel = "ba/b"
    elif fmt.startswith("v") and fmt[1:].isdigit():
        h = int(fmt[1:])
        fsel = f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
    else:
        fsel = "bv*+ba/b"

    opts = ydl_base_opts() | {
        "format": fsel,
        "outtmpl": str(workdir / "%(title).70B.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": item == 0,
    }
    if item:
        opts["playlist_items"] = str(item)
    if fmt == "audio":
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                   "preferredquality": "192"}]

    def run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    try:
        await asyncio.to_thread(run)
    except Exception as ex:  # noqa: BLE001
        _cleanup(workdir)
        raise HTTPException(422, f"Gagal download: {str(ex).splitlines()[0][:200]}")

    files = [f for f in workdir.rglob("*") if f.is_file() and not f.name.endswith(".part")]
    if not files:
        _cleanup(workdir)
        raise HTTPException(500, "File tidak terbentuk.")
    f = max(files, key=lambda p: p.stat().st_size)
    background.add_task(_cleanup, workdir)
    return FileResponse(f, filename=f"ZenithCopy - {clean_name(f.stem)}{f.suffix}",
                        background=background)


@app.get("/api/file")
async def api_file(src: str, sig: str, name: str = "zenith", ext: str = "jpg"):
    """Proxy file langsung (gambar/video dari CDN). Hanya URL yang ditandatangani server."""
    if not hmac.compare_digest(sign(src), sig):
        raise HTTPException(403, "Tanda tangan tidak valid.")
    src = safe_url(src)
    referer = f"{urlparse(src).scheme}://{urlparse(src).hostname}/"

    client = httpx.AsyncClient(follow_redirects=True, timeout=60,
                               headers={"User-Agent": UA, "Referer": referer})
    req = client.build_request("GET", src)
    resp = await client.send(req, stream=True)
    if resp.status_code >= 400:
        await resp.aclose(); await client.aclose()
        raise HTTPException(502, f"Server sumber menolak ({resp.status_code}).")

    async def body():
        try:
            async for chunk in resp.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await resp.aclose(); await client.aclose()

    ext = re.sub(r"[^a-z0-9]", "", ext.lower())[:5] or "bin"
    fname = f"ZenithCopy - {clean_name(name)}.{ext}"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"}
    if "content-length" in resp.headers:
        headers["Content-Length"] = resp.headers["content-length"]
    return StreamingResponse(body(), media_type=resp.headers.get("content-type", "application/octet-stream"),
                             headers=headers)


@app.get("/api/health")
async def health():
    return {"ok": True, "yt_dlp": yt_dlp.version.__version__, "ffmpeg": bool(shutil.which("ffmpeg"))}


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
