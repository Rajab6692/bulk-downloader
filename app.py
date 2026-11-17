import os, re, shutil, tempfile, zipfile
from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
import requests
import yt_dlp

app = Flask(__name__)
CORS(app)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NOCHECK = os.environ.get("YTDLP_NOCHECKCERT", "1") == "1"
COOKIES = os.environ.get("COOKIES_FILE") or None

def ydl_opts(extra=None):
    o = {"quiet": True, "no_warnings": True, "nocheckcertificate": NOCHECK,
         "http_headers": {"User-Agent": UA}}
    if COOKIES and os.path.exists(COOKIES):
        o["cookiefile"] = COOKIES
    if extra: o.update(extra)
    return o

def safe_name(s, fallback="video"):
    s = re.sub(r'[^\w\- ]+', '', (s or "").strip())[:80].strip()
    return s or fallback

def tikwm(url):
    r = requests.get("https://www.tikwm.com/api/", params={"url": url, "hd": 1},
                     headers={"User-Agent": UA}, timeout=30)
    j = r.json()
    if j.get("code") != 0:
        raise Exception(j.get("msg") or "tikwm failed")
    return j["data"]

def check_tiktok(url):
    # Profile/@username link (no /video/) -> attempt flat yt-dlp extraction
    if "/video/" not in url and ("/@" in url or "tiktok.com/@" in url):
        with yt_dlp.YoutubeDL(ydl_opts({"extract_flat": True, "playlistend": 50})) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = info.get("entries") or []
        if not entries:
            raise Exception("No videos found / profile fetch blocked by TikTok")
        return [{"url": e.get("url") or f"https://www.tiktok.com/video/{e.get('id')}",
                 "ok": True, "title": e.get("title") or "", "thumbnail": (e.get("thumbnails") or [{}])[0].get("url") if e.get("thumbnails") else "",
                 "duration": e.get("duration"), "id": str(e.get("id") or "")} for e in entries]
    d = tikwm(url)
    return [{"url": url, "ok": True, "title": d.get("title") or "",
             "thumbnail": d.get("cover") or "", "duration": d.get("duration"),
             "id": str(d.get("id") or "")}]

def check_ytdlp(url):
    with yt_dlp.YoutubeDL(ydl_opts({"extract_flat": "in_playlist"})) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" or "entries" in info and info.get("entries"):
        out = []
        for e in (info.get("entries") or [])[:200]:
            if not e: continue
            u = e.get("url") or e.get("webpage_url") or ""
            if u and not u.startswith("http"): u = f"https://www.youtube.com/watch?v={e.get('id')}"
            out.append({"url": u, "ok": True, "title": e.get("title") or "",
                        "thumbnail": (e.get("thumbnails") or [{}])[0].get("url") if e.get("thumbnails") else "",
                        "duration": e.get("duration"), "id": str(e.get("id") or "")})
        if out: return out
    thumb = info.get("thumbnail") or ""
    heights = sorted({f.get("height") for f in (info.get("formats") or []) if f.get("height")}, reverse=True)
    return [{"url": url, "ok": True, "title": info.get("title") or "", "thumbnail": thumb,
             "duration": info.get("duration"), "id": str(info.get("id") or ""), "heights": heights}]

@app.get("/")
def index():
    for cand in (os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html"),
                 os.path.join(os.path.dirname(__file__), "index.html")):
        if os.path.exists(cand): return send_file(os.path.abspath(cand))
    return jsonify(ok=True, service="bulk-downloader backend")

@app.get("/api/health")
def health(): return jsonify(ok=True)

@app.post("/api/check")
def check():
    body = request.get_json(force=True, silent=True) or {}
    platform = (body.get("platform") or "").lower()
    urls = [u.strip() for u in (body.get("urls") or []) if u and u.strip()]
    results = []
    for url in urls:
        try:
            items = check_tiktok(url) if platform == "tiktok" else check_ytdlp(url)
            results.extend(items)
        except Exception as e:
            msg = str(e)
            if "Sign in to confirm" in msg or "not a bot" in msg:
                msg = "YouTube blocked this server: 'Sign in to confirm you're not a bot' (datacenter IP). Add a cookies.txt on the backend (COOKIES_FILE) or try again later."
            results.append({"url": url, "ok": False, "title": "", "thumbnail": "",
                            "duration": None, "id": "", "error": msg[:500]})
    return jsonify(ok=True, platform=platform, results=results)

def download_tiktok(url, dest_dir, nowatermark=True):
    d = tikwm(url)
    # No-watermark: prefer the HD stream (hdplay) over the standard play URL.
    play = ((d.get("hdplay") or d.get("play")) if nowatermark else (d.get("wmplay") or d.get("play"))) or d.get("play")
    if not play: raise Exception("tikwm returned no play URL")
    if play.startswith("/"): play = "https://www.tikwm.com" + play
    name = safe_name(d.get("title"), f"tiktok-{d.get('id','video')}") + ".mp4"
    path = os.path.join(dest_dir, name)
    with requests.get(play, headers={"User-Agent": UA, "Referer": "https://www.tiktok.com/"}, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(8192): f.write(chunk)
    return path

def download_ytdlp(url, dest_dir, quality="best", platform=""):
    # Default is "best": the highest quality the platform offers, i.e. the video
    # as uploaded. A numeric quality only CAPS the height; it never forces lower
    # when "best" is chosen. (Before 2026-11-17 the default cap was 720p, which
    # silently downgraded Facebook downloads.)
    q = str(quality or "best").lower()
    fmt = {"best": "bestvideo+bestaudio/best",
           "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
           "720": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
           "480": "bestvideo[height<=480]+bestaudio/best[height<=480]/best"}.get(q, "bestvideo+bestaudio/best")
    opts = ydl_opts({"format": fmt, "format_sort": ["res", "fps", "br"], "merge_output_format": "mp4",
                     "outtmpl": os.path.join(dest_dir, "%(title).80s [%(id)s].%(ext)s")})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info).rsplit(".", 1)[0] + ".mp4" if os.path.exists(ydl.prepare_filename(info).rsplit(".",1)[0]+".mp4") else ydl.prepare_filename(info)

@app.post("/api/zip")
def make_zip():
    body = request.get_json(force=True, silent=True) or {}
    platform = (body.get("platform") or "").lower()
    quality = body.get("quality") or "best"
    nowm = body.get("nowatermark", True)
    items = body.get("items") or []
    tmp = tempfile.mkdtemp(prefix="dl-")
    failed, got = [], []
    try:
        for i, it in enumerate(items):
            url, title = it.get("url"), it.get("title") or ""
            sub = os.path.join(tmp, str(i)); os.makedirs(sub)
            try:
                p = download_tiktok(url, sub, nowm) if platform == "tiktok" else download_ytdlp(url, sub, quality, platform)
                for root, _, files in os.walk(sub):
                    for fn in files: got.append(os.path.join(root, fn))
                if not any(sub in g for g in got): raise Exception("download produced no file")
            except Exception as e:
                failed.append(f"{url} — {str(e)[:300]}")
        if failed:
            with open(os.path.join(tmp, "_FAILED.txt"), "w") as f:
                f.write("These items failed:\n" + "\n".join(failed))
        zpath = os.path.join(tempfile.gettempdir(), f"{platform or 'videos'}-download.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            seen = {}
            for p in got:
                arc = os.path.basename(p)
                if arc in seen: seen[arc]+=1; arc = f"{seen[arc]}-{arc}"
                else: seen[arc]=0
                z.write(p, arc)
            if failed: z.write(os.path.join(tmp, "_FAILED.txt"), "_FAILED.txt")
        @after_this_request
        def cleanup(resp):
            shutil.rmtree(tmp, ignore_errors=True)
            try: os.remove(zpath)
            except OSError: pass
            return resp
        return send_file(zpath, as_attachment=True, download_name=f"{platform or 'videos'}-download.zip", mimetype="application/zip")
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify(ok=False, error=str(e)[:500]), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
