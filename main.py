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
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zenith")

BASE = Path(__file__).parent
TMP = Path(tempfile.gettempdir()) / "zenith_copy"
TMP.mkdir(exist_ok=True)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


_secret = os.environ.get("ZENITH_SECRET")
if not _secret:
    log.warning("ZENITH_SECRET belum di-set: link download gambar akan kedaluwarsa setiap server restart.")
SECRET = (_secret or uuid.uuid4().hex).encode()
COOKIES = os.environ.get("COOKIES_FILE")  # opsional: cookies.txt (untuk IG/FB yang minta login)

MAX_ITEMS = 30
MAX_FILESIZE = env_int("MAX_FILESIZE_MB", 500) * 1024 * 1024   # batas ukuran 1 file
MAX_DURATION = env_int("MAX_DURATION_SEC", 2 * 60 * 60)         # batas durasi video (detik)
DOWNLOAD_TIMEOUT = env_int("DOWNLOAD_TIMEOUT_SEC", 10 * 60)     # batas waktu proses download
MAX_CONCURRENT_DOWNLOADS = env_int("MAX_CONCURRENT_DOWNLOADS", 3)
MAX_CONCURRENT_INFO = env_int("MAX_CONCURRENT_INFO", 5)
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"         # set 1 kalau di belakang proxy (Railway/Render)
ALLOW_GENERIC = os.environ.get("ALLOW_GENERIC", "0") == "1"     # extractor "generic" yt-dlp = fetch URL bebas
TMP_MAX_AGE = 60 * 60

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

VIDEO_EXT = {"mp4", "webm", "mov", "mkv", "m4v"}
IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif", "heic"}
AUDIO_EXT = {"mp3", "m4a", "aac", "ogg", "opus", "wav"}

# rate limit: bucket -> (maks request, per detik)
RATE = {"info": (20, 60), "download": (8, 60), "file": (60, 60)}
_hits: dict[tuple[str, str], deque] = defaultdict(deque)

DOWNLOAD_SEM = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
INFO_SEM = asyncio.Semaphore(MAX_CONCURRENT_INFO)

app = FastAPI(title="Zenith Copy")


# ---------------------------------------------------------------- utils
def _ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def safe_url(u: str) -> str:
    """Hanya http/https ke alamat publik (cegah SSRF ke jaringan internal)."""
    u = (u or "").strip()
    if len(u) > 2048:
        raise HTTPException(400, "Link terlalu panjang.")
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "Link tidak valid. Pastikan diawali http:// atau https://")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or None)
    except (socket.gaierror, UnicodeError):
        raise HTTPException(400, "Domain tidak ditemukan.")
    for info in infos:
        if not _ip_allowed(ipaddress.ip_address(info[4][0])):
            raise HTTPException(400, "Alamat tidak diizinkan.")
    return u


async def safe_url_async(u: str) -> str:
    return await asyncio.to_thread(safe_url, u)


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


def safe_ext(ext: str, fallback="bin") -> str:
    return re.sub(r"[^a-z0-9]", "", (ext or "").lower())[:5] or fallback


def ydl_base_opts() -> dict:
    o = {"quiet": True, "no_warnings": True, "http_headers": {"User-Agent": UA},
         "socket_timeout": 20}
    if not ALLOW_GENERIC:
        # extractor "generic" bisa mengambil URL apa saja (termasuk redirect ke jaringan internal)
        o["allowed_extractors"] = ["default", "-generic"]
    if COOKIES and Path(COOKIES).exists():
        o["cookiefile"] = COOKIES
    return o


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[-1].strip()  # entri terakhir = ditambahkan proxy platform
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, bucket: str):
    limit, window = RATE[bucket]
    now = time.monotonic()
    if len(_hits) > 5000:  # bersihkan entri lama supaya memori tidak membengkak
        for k in [k for k, q in _hits.items() if not q or now - q[-1] > 120]:
            _hits.pop(k, None)
    q = _hits[(bucket, client_ip(request))]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Terlalu banyak permintaan. Coba lagi sebentar.")
    q.append(now)


@asynccontextmanager
async def slot(sem: asyncio.Semaphore, wait: float = 15):
    try:
        await asyncio.wait_for(sem.acquire(), wait)
    except asyncio.TimeoutError:
        raise HTTPException(503, "Server sedang sibuk, coba lagi sebentar.")
    try:
        yield
    finally:
        sem.release()


def cleanup_old_tmp():
    """Hapus folder kerja sisa (misal karena server mati di tengah download)."""
    now = time.time()
    for d in TMP.iterdir():
        try:
            if now - d.stat().st_mtime > TMP_MAX_AGE:
                shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)
        except OSError:
            pass


cleanup_old_tmp()


# ------------------------------------------------------------ extractors
def info_ytdlp(url: str) -> dict | None:
    opts = ydl_base_opts() | {"skip_download": True, "extract_flat": False,
                              "playlistend": MAX_ITEMS}
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
        direct_ext = safe_ext(e.get("ext"), "")

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
                            "sig": sign(thumb), "ext": safe_ext(ext_of(thumb), "jpg")})

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
            ext = safe_ext(meta.get("extension") or ext_of(u), "jpg")
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
async def api_info(req: InfoReq, request: Request):
    rate_limit(request, "info")
    url = await safe_url_async(req.url)
    async with slot(INFO_SEM):
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
                log.info("info %s gagal untuk %s: %s", fn.__name__, url, str(ex).split("\n")[0][:300])
    msg = "Media tidak ditemukan. Link mungkin private, butuh login, atau tidak didukung."
    raise HTTPException(422, {"message": msg})


def _cleanup(path: Path):
    shutil.rmtree(path, ignore_errors=True)


class _Abort(Exception):
    pass


@app.get("/api/download")
async def api_download(request: Request, background: BackgroundTasks,
                       url: str = Query(...), fmt: str = "vbest", item: int = Query(0, ge=0, le=MAX_ITEMS)):
    rate_limit(request, "download")
    url = await safe_url_async(url)
    await asyncio.to_thread(cleanup_old_tmp)

    if fmt == "audio":
        fsel = "ba/b"
    elif fmt.startswith("v") and fmt[1:].isdigit():
        h = int(fmt[1:])
        fsel = f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
    else:
        fsel = "bv*+ba/b"

    state = {"reason": None}  # diisi kalau download dihentikan paksa

    def guard(d):
        if state["reason"]:
            raise _Abort(state["reason"])
        done = d.get("downloaded_bytes") or 0
        if done > MAX_FILESIZE:
            state["reason"] = "size"
            raise _Abort("size")

    async with slot(DOWNLOAD_SEM):
        workdir = TMP / uuid.uuid4().hex
        workdir.mkdir()
        opts = ydl_base_opts() | {
            "format": fsel,
            "outtmpl": str(workdir / "%(title).70B.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": item == 0,
            "max_filesize": MAX_FILESIZE,
            "match_filter": yt_dlp.utils.match_filter_func(f"!is_live & duration <=? {MAX_DURATION}"),
            "progress_hooks": [guard],
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
            await asyncio.wait_for(asyncio.to_thread(run), DOWNLOAD_TIMEOUT)
        except asyncio.TimeoutError:
            state["reason"] = "timeout"  # thread berhenti di progress hook berikutnya
            _cleanup(workdir)
            raise HTTPException(504, "Download terlalu lama dan dihentikan.")
        except Exception as ex:  # noqa: BLE001
            _cleanup(workdir)
            if state["reason"] == "size":
                raise HTTPException(413, f"File melebihi batas {MAX_FILESIZE // (1024 * 1024)} MB.")
            log.info("download gagal untuk %s: %s", url, str(ex).splitlines()[0][:300] if str(ex) else ex)
            raise HTTPException(422, "Gagal download. Link mungkin private, butuh login, atau tidak didukung.")

    files = [f for f in workdir.rglob("*") if f.is_file() and not f.name.endswith(".part")]
    if not files:
        _cleanup(workdir)
        raise HTTPException(413, f"File tidak bisa diunduh: melebihi batas ukuran "
                                 f"({MAX_FILESIZE // (1024 * 1024)} MB) / durasi "
                                 f"({MAX_DURATION // 60} menit), atau berupa siaran live.")
    f = max(files, key=lambda p: p.stat().st_size)
    background.add_task(_cleanup, workdir)
    return FileResponse(f, filename=f"ZenithCopy - {clean_name(f.stem)}{f.suffix}",
                        background=background)


@app.get("/api/file")
async def api_file(request: Request, src: str, sig: str, name: str = "zenith", ext: str = "jpg"):
    """Proxy file langsung (gambar/video dari CDN). Hanya URL yang ditandatangani server."""
    rate_limit(request, "file")
    if not hmac.compare_digest(sign(src), sig):
        raise HTTPException(403, "Tanda tangan tidak valid.")
    target = await safe_url_async(src)
    referer = f"{urlparse(src).scheme}://{urlparse(src).hostname}/"

    # redirect diikuti manual supaya tiap lompatan dicek lagi oleh safe_url (anti-SSRF)
    client = httpx.AsyncClient(follow_redirects=False, timeout=60,
                               headers={"User-Agent": UA, "Referer": referer})
    resp = None
    try:
        for _ in range(6):
            resp = await client.send(client.build_request("GET", target), stream=True)
            if not resp.is_redirect:
                break
            location = resp.headers.get("location")
            next_url = str(resp.url.join(location)) if location else None
            await resp.aclose()
            resp = None
            if not next_url:
                raise HTTPException(502, "Redirect tidak valid dari server sumber.")
            target = await safe_url_async(next_url)
        else:
            raise HTTPException(502, "Terlalu banyak redirect.")
        if resp is None or resp.status_code >= 400:
            code = resp.status_code if resp is not None else "?"
            raise HTTPException(502, f"Server sumber menolak ({code}).")
        length = resp.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_FILESIZE:
            raise HTTPException(413, f"File melebihi batas {MAX_FILESIZE // (1024 * 1024)} MB.")
    except HTTPException:
        if resp is not None:
            await resp.aclose()
        await client.aclose()
        raise
    except httpx.HTTPError:
        if resp is not None:
            await resp.aclose()
        await client.aclose()
        raise HTTPException(502, "Gagal menghubungi server sumber.")

    async def body():
        sent = 0
        try:
            async for chunk in resp.aiter_bytes(64 * 1024):
                sent += len(chunk)
                if sent > MAX_FILESIZE:
                    break
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    fname = f"ZenithCopy - {clean_name(name)}.{safe_ext(ext)}"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"}
    if length and length.isdigit():
        headers["Content-Length"] = length
    return StreamingResponse(body(), media_type=resp.headers.get("content-type", "application/octet-stream"),
                             headers=headers)


@app.get("/api/health")
async def health():
    return {"ok": True, "yt_dlp": yt_dlp.version.__version__, "ffmpeg": bool(shutil.which("ffmpeg"))}


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=env_int("PORT", 8000))
