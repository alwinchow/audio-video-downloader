import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

# Use the OS certificate store so corporate TLS-inspection CAs are trusted.
# Harmless on Azure/Linux (falls back to the system store there too).
import truststore
truststore.inject_into_ssl()

from flask import Flask, request, jsonify, send_from_directory, send_file
import yt_dlp

import sys

# Two different "here"s once this can run as a packaged .exe:
#   BASE_DIR — read-only resources (index.html). Inside a PyInstaller bundle
#              these live in a temp extraction folder (sys._MEIPASS), not next
#              to the .exe.
#   DATA_DIR — writable, persistent storage (downloads/, cookies.txt). Must
#              NOT be the temp extraction folder (wiped every run) or next to
#              the .exe (likely Program Files, no write access without admin).
#              %LOCALAPPDATA% is the standard writable location for this.
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "YTDownloader"
else:
    BASE_DIR = Path(__file__).parent
    DATA_DIR = BASE_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR = DATA_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

INDEX_FILE = "index.html"

# Shared access code. Set ACCESS_CODE in Azure App Settings; this default is
# only for local testing.
ACCESS_CODE = os.environ.get("ACCESS_CODE", "family123")


def cookie_store():
    """Where uploaded cookies live.

    On Azure, /home is the one writable place that survives both restarts and
    redeploys — the app directory is replaced on every deploy, so cookies kept
    there would vanish each time you push. WEBSITE_SITE_NAME is only set by App
    Service, so locally this stays beside the app instead of scattering files
    into the user's home directory.
    """
    if os.environ.get("WEBSITE_SITE_NAME"):
        store = Path(os.environ.get("HOME", "/home")) / "data"
        try:
            store.mkdir(parents=True, exist_ok=True)
            return store / "cookies.txt"
        except OSError:
            pass
    return DATA_DIR / "cookies.txt"


COOKIE_PATH = cookie_store()
COOKIE_LOCK = threading.Lock()


def load_cookiefile():
    """Find the YouTube session cookies for yt-dlp, if there are any.

    YouTube blocks datacenter IPs with "Sign in to confirm you're not a bot",
    so a cloud-hosted instance needs a signed-in session to work at all.
    Cookies can come from the YT_COOKIES app setting or be uploaded through the
    page; the uploaded copy wins, since that's the one you can refresh without
    a redeploy. They are never committed — these are full account credentials.
    """
    if COOKIE_PATH.exists() and COOKIE_PATH.stat().st_size > 0:
        return str(COOKIE_PATH)
    raw = os.environ.get("YT_COOKIES", "").strip()
    if raw:
        try:
            # App Settings mangle real newlines, so accept escaped ones too.
            COOKIE_PATH.write_text(raw.replace("\\n", "\n"), encoding="utf-8")
            return str(COOKIE_PATH)
        except OSError:
            fd, path = tempfile.mkstemp(prefix="ytc_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(raw.replace("\\n", "\n"))
            return path
    return None


COOKIEFILE = load_cookiefile()


# The cookies that actually keep you signed in. YouTube also sets short-lived
# ones (GPS lasts ~30 minutes, CONSISTENCY similar) which say nothing about
# whether the session is still good — reporting the earliest expiry across all
# of them just produces an alarming "0 days" on a perfectly healthy export.
AUTH_COOKIES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID",
}


def cookie_status():
    """Describe the stored cookies without ever revealing them."""
    if not COOKIEFILE or not Path(COOKIEFILE).exists():
        return {"loaded": False, "message": "No YouTube sign-in saved."}

    youtube = 0
    found_auth = set()
    soonest = None
    now = time.time()
    try:
        for line in Path(COOKIEFILE).read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7 or "youtube" not in parts[0]:
                continue
            youtube += 1
            name = parts[5].strip()
            if name not in AUTH_COOKIES:
                continue
            found_auth.add(name)
            try:
                expiry = int(parts[4])
            except ValueError:
                continue
            # 0 means a session cookie, which has no useful expiry to report.
            if expiry > now and (soonest is None or expiry < soonest):
                soonest = expiry
    except OSError as e:
        return {"loaded": False, "message": f"Could not read the saved cookies: {e}"}

    if not youtube:
        return {"loaded": False,
                "message": "The saved file has no youtube.com cookies in it."}
    if not found_auth:
        return {"loaded": False,
                "message": f"{youtube} YouTube cookies saved, but none of the sign-in "
                           f"ones. Make sure you were logged in when you exported."}

    msg = f"Signed in — {youtube} cookies saved, {len(found_auth)} of them sign-in."
    if soonest:
        days = int((soonest - now) // 86400)
        when = time.strftime("%d %b %Y", time.localtime(soonest))
        msg += (f" Sign-in expires {when}"
                f" ({days} days)." if days > 0 else f" Sign-in expires {when} — today.")
    return {"loaded": True, "message": msg, "count": youtube,
            "auth": sorted(found_auth)}

# Which YouTube client yt-dlp pretends to be, as an escape hatch via the
# YT_PLAYER_CLIENTS app setting (comma-separated).
#
# Deliberately defaults to yt-dlp's own choice. Forcing specific clients was
# tried as a way around YouTube's datacenter-IP blocking and measured worse:
# from an unblocked connection, tv / mweb / web_safari / web all returned no
# usable formats, and android_vr returned a single video-only stream with no
# audio at all — which would silently break audio downloads. Those clients now
# need PO tokens yt-dlp can't produce on its own.
#
# Kept configurable because what YouTube accepts shifts over time, but only
# change it if you can verify formats still come back.
PLAYER_CLIENTS = [
    c.strip() for c in
    os.environ.get("YT_PLAYER_CLIENTS", "default").split(",")
    if c.strip()
]


def extractor_args():
    if not PLAYER_CLIENTS or PLAYER_CLIENTS == ["default"]:
        return {}
    return {"extractor_args": {"youtube": {"player_client": PLAYER_CLIENTS}}}


# Route YouTube traffic through a proxy, set as YT_PROXY. A *residential*
# proxy is the point: YouTube blocks datacenter IPs, so a datacenter proxy
# just swaps one blocked address for another.
#
# Format: http://user:pass@host:port (or socks5://...). These providers bill
# per gigabyte and this app downloads the whole file before trimming, so a
# short clip from a long video still costs the full download.
YT_PROXY = os.environ.get("YT_PROXY", "").strip()


def proxy_args():
    return {"proxy": YT_PROXY} if YT_PROXY else {}


def proxy_label():
    """Describe the proxy without leaking its credentials."""
    if not YT_PROXY:
        return None
    shown = YT_PROXY
    if "@" in shown:                      # strip user:pass
        shown = shown.split("://", 1)[0] + "://***@" + shown.rsplit("@", 1)[1]
    return shown

# Keep the free-tier 1 GB disk from filling up.
MAX_FILES = 20
MAX_AGE_SECONDS = 60 * 60  # delete finished files older than 1 hour
# Videos are far bigger than audio, so cap the folder by size too — a handful
# of 1080p downloads would otherwise blow past the disk on their own.
MAX_TOTAL_BYTES = 700 * 1024 * 1024

# In-memory map of stored filename -> original title (for a friendly download
# name). Lost on restart, in which case we fall back to "audio".
TITLES = {}

AUDIO_EXTS = {".mp3", ".m4a", ".webm", ".opus", ".ogg", ".aac"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}
SERVABLE_EXTS = AUDIO_EXTS | VIDEO_EXTS

# Content types the browser's <audio> element understands, per extension.
MIME_BY_EXT = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".webm": "audio/webm",
    ".opus": "audio/ogg", ".ogg": "audio/ogg", ".aac": "audio/aac",
}

def find_ffmpeg():
    """Locate ffmpeg: system install, then a path bundled next to this file,
    then the imageio-ffmpeg wheel's own binary.

    Azure's Python containers have no ffmpeg, and apt-get installing it on every
    container start was slow and could fail outright — taking the whole app down
    with it. The imageio-ffmpeg wheel ships a static binary, so pip alone is
    enough there and there's nothing to install at boot.
    PyInstaller is a second case: imageio_ffmpeg's own resource lookup isn't
    guaranteed to survive being frozen into a bundle, so the build step places
    a copy at BASE_DIR/ffmpeg/ffmpeg(.exe) as a path this code controls
    directly, independent of that package's internals.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    bundled = BASE_DIR / "ffmpeg" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if bundled.exists():
        return str(bundled)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


FFMPEG = find_ffmpeg()

# Preview recipes, keyed by the codec pair a browser says it can play. The
# first three need no re-encoding — YouTube already publishes those streams —
# so we only pay for ffmpeg when a browser insists on something exotic.
LOW = "[height<=240]"
PREVIEW_RECIPES = {
    "vp9_opus": {
        "format": (f"worstvideo{LOW}[ext=webm]+worstaudio[ext=webm]/"
                   f"worstvideo{LOW}+worstaudio/worst{LOW}/worst"),
        "container": "webm", "audio": None,
    },
    "h264_aac": {
        "format": (f"worstvideo{LOW}[ext=mp4]+worstaudio[ext=m4a]/"
                   f"worst{LOW}[ext=mp4]/worstvideo{LOW}+worstaudio/worst"),
        "container": "mp4", "audio": None,
    },
    "h264_mp3": {
        "format": (f"worstvideo{LOW}[ext=mp4]+worstaudio[ext=m4a]/"
                   f"worst{LOW}[ext=mp4]/worstvideo{LOW}+worstaudio/worst"),
        "container": "mp4", "audio": "mp3",   # keep the picture, re-encode sound
    },
    "vp8_vorbis": {
        "format": (f"worstvideo{LOW}[ext=webm]+worstaudio[ext=webm]/"
                   f"worstvideo{LOW}+worstaudio/worst{LOW}/worst"),
        "container": "webm", "audio": "vorbis",
    },
}

# Live progress per job, so the page can show a real bar instead of a spinner.
# The client makes up a job id and polls /progress/<id> while it waits.
PROGRESS = {}
PROGRESS_LOCK = threading.Lock()
JOB_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def job_from_request():
    job = (request.form.get("job") or "").strip()
    return job if JOB_RE.match(job) else None


def set_progress(job, **fields):
    if not job:
        return
    with PROGRESS_LOCK:
        entry = PROGRESS.setdefault(job, {})
        entry.update(fields)
        entry["at"] = time.time()
        # Drop anything stale so the dict can't grow without bound.
        for key in [k for k, v in PROGRESS.items() if time.time() - v.get("at", 0) > 3600]:
            PROGRESS.pop(key, None)


def dl_hook(job, stage, parts=1):
    """yt-dlp progress callback -> our progress dict.

    A merged video is fetched as two streams (picture, then sound), so the
    percentage restarts partway. `parts` lets the label say which one we're on
    instead of looking like the bar went backwards.
    """
    done_parts = [0]

    def hook(d):
        label = stage if parts < 2 else f"{stage} ({min(done_parts[0] + 1, parts)} of {parts})"
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            set_progress(
                job, stage=label,
                percent=round(done / total * 100, 1) if total else 0,
                done_mb=round(done / 1048576, 1),
                total_mb=round(total / 1048576, 1) if total else 0,
                speed_mbps=round((d.get("speed") or 0) / 1048576, 2),
            )
        elif d.get("status") == "finished":
            done_parts[0] += 1
            if done_parts[0] >= parts:
                set_progress(job, stage="Merging", percent=100, speed_mbps=0)
    return hook


def run_ffmpeg(cmd, job=None, total_seconds=0, stage="Converting"):
    """Run ffmpeg, reporting progress against an expected output length."""
    if job and total_seconds:
        cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
        set_progress(job, stage=stage, percent=0, speed_mbps=0)
    # stderr goes to a file: reading two pipes here risks deadlocking on a
    # full buffer, and we only need stderr if the run fails.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True)
        try:
            if proc.stdout:
                for line in proc.stdout:
                    if line.startswith("out_time_us=") and job and total_seconds:
                        try:
                            secs = int(line.split("=", 1)[1]) / 1_000_000
                        except ValueError:
                            continue
                        set_progress(job, stage=stage,
                                     percent=round(min(secs / total_seconds * 100, 100), 1))
            proc.wait(timeout=1800)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        errf.seek(0)
        return proc.returncode, errf.read()

app = Flask(__name__)

YOUTUBE_RE = re.compile(r"^https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/")
ID_RE = re.compile(r"^[0-9a-f]{32}$")


def code_ok():
    return (request.form.get("code") or "").strip() == ACCESS_CODE


def find_source(file_id):
    """The prepared audio file for an id, or None if it's gone/invalid."""
    if not ID_RE.match(file_id or ""):
        return None
    hits = [p for p in DOWNLOAD_DIR.glob(f"{file_id}.*") if p.suffix.lower() in AUDIO_EXTS]
    return hits[0] if hits else None


def stamp(seconds):
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def friendly_error(exc):
    """Turn yt-dlp's raw error into something a visitor can act on."""
    text = str(exc)
    low = text.lower()
    # Age-gating first: "Sign in to confirm your age" and "Sign in to confirm
    # you're not a bot" both contain "sign in to confirm", so the broader rule
    # below would otherwise swallow it and blame the wrong thing.
    if "age-restricted" in low or "confirm your age" in low:
        return "That video is age-restricted, so it needs a signed-in session."
    if "not a bot" in low or "sign in to confirm" in low:
        if COOKIEFILE:
            return ("YouTube rejected the saved sign-in — the cookies have most "
                    "likely expired. Export a fresh cookies.txt and update the "
                    "YT_COOKIES setting.")
        return ("YouTube is blocking this server as a bot. It needs a signed-in "
                "session: export a cookies.txt and put it in the YT_COOKIES "
                "setting.")
    # Match whole phrases, not fragments: "age" alone also matches "page", and
    # "confirm" appears in yt-dlp's own "Confirm you are on the latest version"
    # boilerplate, which together mislabelled unrelated failures as age-gating.
    if "proxy" in low or "proxyerror" in low:
        return ("Could not reach the proxy. Check the YT_PROXY setting — host, "
                "port and credentials.")
    if "private video" in low:
        return "That video is private."
    if "video unavailable" in low:
        return "That video is unavailable (it may be removed or region-locked)."
    if "age-restricted" in low or "confirm your age" in low:
        return "That video is age-restricted, so it needs a signed-in session."
    return text[:150]


def make_preview(src, file_id, job=None, duration=0):
    """Build an MP3 copy for in-browser playback.

    The source is usually Opus/webm, which some embedded browsers (VS Code's
    viewer, codec-stripped Chromium builds) can't decode — the audio simply
    never loads. MP3 plays everywhere. This is only ever built from the small
    preview-quality fetch; the real download fetches full quality separately.
    """
    out = DOWNLOAD_DIR / f"{file_id}_preview.mp3"
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        # 96k keeps the preview small so it loads fast on a phone; it only has
        # to be good enough to pick cut points by ear.
        "-vn", "-c:a", "libmp3lame", "-b:a", "96k", "-write_xing", "1", str(out),
    ]
    try:
        rc, _ = run_ffmpeg(cmd, job, duration, stage="Preparing preview")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return out if rc == 0 and out.exists() else None


def cleanup_downloads(keep=()):
    """Delete old / excess files so the disk never fills.

    `keep` names files the caller just produced — a single big video can exceed
    the size cap on its own, and we must not delete the very file we're about
    to hand back.
    """
    now = time.time()
    files = sorted(
        (p for p in DOWNLOAD_DIR.glob("*.*") if p.name not in keep),
        key=lambda p: p.stat().st_mtime,
    )
    for p in list(files):
        try:
            if now - p.stat().st_mtime > MAX_AGE_SECONDS:
                p.unlink()
                files.remove(p)
        except OSError:
            pass
    def total():
        return sum(p.stat().st_size for p in files if p.exists())

    while files and (len(files) > MAX_FILES or total() > MAX_TOTAL_BYTES):
        victim = files.pop(0)
        try:
            victim.unlink()
        except OSError:
            pass


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, INDEX_FILE)


@app.route("/cookies", methods=["POST"])
def cookies_upload():
    """Save an uploaded cookies.txt so YouTube treats us as signed in.

    Behind the access code: anyone who can use the app can replace the session,
    which is the right trade for a shared family instance. The contents are
    never echoed back — only a count and the expiry date.
    """
    global COOKIEFILE
    if not code_ok():
        return jsonify(status="error", message="Wrong access code."), 403

    action = (request.form.get("action") or "save").strip()

    if action == "status":
        return jsonify(status="success", proxy=proxy_label(), **cookie_status())

    if action == "clear":
        with COOKIE_LOCK:
            try:
                COOKIE_PATH.unlink(missing_ok=True)
            except OSError as e:
                return jsonify(status="error", message=f"Could not remove: {e}"), 500
            COOKIEFILE = load_cookiefile()
        return jsonify(status="success", **cookie_status())

    if action == "test":
        if not COOKIEFILE:
            return jsonify(status="error", message="No sign-in saved to test."), 400
        # A real extraction is the only honest check — the file can look fine
        # and still be rejected by YouTube.
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                "skip_download": True, "cookiefile": COOKIEFILE, **extractor_args(), **proxy_args()}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info("https://www.youtube.com/watch?v=jNQXAC9IVRw",
                                        download=False)
        except Exception as e:
            return jsonify(status="error", message=friendly_error(e)), 502
        return jsonify(status="success",
                       message=f"Working — YouTube answered normally "
                               f"(read \"{info.get('title', '?')}\").")

    raw = (request.form.get("cookies") or "").strip()
    if not raw:
        return jsonify(status="error", message="No file contents received."), 400
    if "youtube" not in raw:
        return jsonify(status="error",
                       message="That file has no youtube.com cookies in it. Make sure "
                               "you exported for youtube.com in Netscape format."), 400
    if len(raw) > 1_000_000:
        return jsonify(status="error", message="That file is implausibly large."), 400

    with COOKIE_LOCK:
        try:
            COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
            COOKIE_PATH.write_text(raw if raw.endswith("\n") else raw + "\n",
                                   encoding="utf-8")
            try:
                os.chmod(COOKIE_PATH, 0o600)   # best effort; not all mounts honour it
            except OSError:
                pass
        except OSError as e:
            return jsonify(status="error", message=f"Could not save: {e}"), 500
        COOKIEFILE = load_cookiefile()

    return jsonify(status="success", **cookie_status())


@app.route("/progress/<job>")
def progress(job):
    """Where a running download/convert has got to, for the progress bar."""
    if not JOB_RE.match(job):
        return jsonify(stage="unknown", percent=0)
    with PROGRESS_LOCK:
        return jsonify(PROGRESS.get(job) or {"stage": "Starting", "percent": 0})


@app.route("/formats", methods=["POST"])
def formats():
    """Read the video (no download) and return selectable quality options."""
    if not code_ok():
        return jsonify(status="error", message="Wrong access code."), 403

    url = (request.form.get("url") or "").strip()
    if not YOUTUBE_RE.match(url):
        return jsonify(status="error", message="That doesn't look like a YouTube link."), 400

    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "skip_download": True, "cookiefile": COOKIEFILE, **extractor_args(), **proxy_args()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(status="error", message=f"Could not read video: {friendly_error(e)}"), 500

    # Best audio bitrate the source actually offers.
    abrs = [
        f.get("abr") or 0
        for f in info.get("formats", [])
        if f.get("acodec") not in (None, "none")
    ]
    max_abr = int(max(abrs)) if abrs else 0

    options = [
        {"id": "mp3_320", "label": "MP3 — 320 kbps (best)"},
        {"id": "mp3_192", "label": "MP3 — 192 kbps (recommended)"},
        {"id": "mp3_128", "label": "MP3 — 128 kbps (smaller file)"},
        {"id": "original", "label": "Original audio — fastest, no conversion"},
    ]

    return jsonify(
        status="success",
        title=info.get("title", "audio"),
        thumbnail=info.get("thumbnail", ""),
        uploader=info.get("uploader", ""),
        duration=int(info.get("duration") or 0),
        best_abr=max_abr,
        options=options,
    )


@app.route("/video-formats", methods=["POST"])
def video_formats():
    """Read the video (no download) and list the resolutions it offers."""
    if not code_ok():
        return jsonify(status="error", message="Wrong access code."), 403

    url = (request.form.get("url") or "").strip()
    if not YOUTUBE_RE.match(url):
        return jsonify(status="error", message="That doesn't look like a YouTube link."), 400

    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "skip_download": True, "cookiefile": COOKIEFILE, **extractor_args(), **proxy_args()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(status="error", message=f"Could not read video: {friendly_error(e)}"), 500

    heights = sorted({
        f["height"] for f in info.get("formats", [])
        if f.get("vcodec") not in (None, "none") and f.get("height")
    }, reverse=True)

    options = [{"id": "best", "label": "Best available"}]
    for h in heights:
        tag = " (4K)" if h >= 2160 else " (HD)" if h >= 720 else ""
        options.append({"id": str(h), "label": f"{h}p{tag}"})

    return jsonify(
        status="success",
        title=info.get("title", "video"),
        thumbnail=info.get("thumbnail", ""),
        uploader=info.get("uploader", ""),
        duration=int(info.get("duration") or 0),
        best_height=heights[0] if heights else 0,
        options=options,
    )


@app.route("/video-prepare", methods=["POST"])
def video_prepare():
    """Fetch a small 360p copy purely so the browser can preview and trim it.

    WebM (VP9/Opus) is preferred: it's royalty-free, so it plays even in the
    codec-stripped browsers that can't decode H.264/AAC. The real download is
    taken separately at full quality, so this never limits what you get.
    """
    job = job_from_request()
    if not code_ok():
        return jsonify(status="error", message="Wrong access code."), 403

    url = (request.form.get("url") or "").strip()
    if not YOUTUBE_RE.match(url):
        return jsonify(status="error", message="That doesn't look like a YouTube link."), 400

    # The page tells us what its browser can actually decode; we hand back that
    # combination rather than assuming. Browsers vary wildly here — some builds
    # play MP3 but refuse VP9/Opus or AAC.
    support = (request.form.get("support") or "").split(",")
    support = [s.strip() for s in support if s.strip() in PREVIEW_RECIPES]
    kind = support[0] if support else "vp9_opus"
    recipe = PREVIEW_RECIPES[kind]

    file_id = uuid.uuid4().hex
    opts = {
        "outtmpl": str(DOWNLOAD_DIR / f"{file_id}_vpreview.%(ext)s"),
        # Lowest resolution that exists: the preview only has to be good enough
        # to recognise where you are. An hour at 360p is ~200 MB, which is slow
        # to fetch and chokes some embedded browsers; 144p is a fraction of it.
        "format": recipe["format"],
        "merge_output_format": recipe["container"],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "concurrent_fragment_downloads": 8,
        "ffmpeg_location": FFMPEG,
        "cookiefile": COOKIEFILE,
        **extractor_args(),
        **proxy_args(),
        "progress_hooks": [dl_hook(job, "Fetching preview", parts=2)],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        return jsonify(status="error", message=f"Preview download failed: {friendly_error(e)}"), 500
    except Exception as e:
        return jsonify(status="error", message=f"Unexpected error: {friendly_error(e)}"), 500

    produced = [
        p for p in DOWNLOAD_DIR.glob(f"{file_id}_vpreview.*")
        if p.suffix.lower() in VIDEO_EXTS
    ]
    if not produced:
        return jsonify(status="error", message="No preview file was produced."), 500

    out = max(produced, key=lambda p: p.stat().st_size)

    # Some browsers play the picture but not the sound it came with. Swapping
    # just the audio track is quick — the video is copied untouched.
    if recipe["audio"]:
        codec = "libmp3lame" if recipe["audio"] == "mp3" else "libvorbis"
        fixed = DOWNLOAD_DIR / f"{file_id}b_vpreview.{recipe['container']}"
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(out),
            "-c:v", "copy", "-c:a", codec, "-b:a", "96k", str(fixed),
        ]
        try:
            rc, _ = run_ffmpeg(cmd, job, int(info.get("duration") or 0), stage="Preparing preview")
            if rc == 0 and fixed.exists():
                out.unlink(missing_ok=True)
                out = fixed
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass   # fall back to the untouched download

    cleanup_downloads(keep={out.name})
    return jsonify(
        status="success",
        stream_url=f"/stream/{out.name}",
        duration=int(info.get("duration") or 0),
        size_mb=round(out.stat().st_size / (1024 * 1024), 1),
        kind=kind,
    )


@app.route("/video-download", methods=["POST"])
def video_download():
    """Download video + audio at the chosen resolution, merged into one file.

    An optional [start, end] is cut out afterwards. The cut re-encodes so the
    clip starts exactly where asked — a stream copy would jump back to the
    nearest keyframe, which can be seconds early.
    """
    job = job_from_request()
    if not code_ok():
        return jsonify(status="error", message="Wrong access code."), 403

    url = (request.form.get("url") or "").strip()
    if not YOUTUBE_RE.match(url):
        return jsonify(status="error", message="That doesn't look like a YouTube link."), 400

    trimmed = (request.form.get("trimmed") or "") == "1"
    start = end = 0.0
    if trimmed:
        try:
            start = max(0.0, float(request.form.get("start") or 0))
            end = float(request.form.get("end") or 0)
        except ValueError:
            return jsonify(status="error", message="Those trim points aren't valid."), 400
        if end - start < 0.1:
            return jsonify(status="error", message="The end must come after the start."), 400

    quality = (request.form.get("quality") or "best").strip()
    # H.264 (avc1) first: YouTube also offers AV1/VP9, which plenty of phones
    # and older players can't decode. Falling back keeps odd videos working.
    if quality == "best":
        fmt = ("bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/"
               "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best")
    else:
        if not quality.isdigit():
            return jsonify(status="error", message="Unknown quality."), 400
        h = int(quality)
        # Prefer mp4+m4a so the merge is a clean remux with no re-encode.
        fmt = (f"bestvideo[height<={h}][vcodec^=avc1]+bestaudio[ext=m4a]/"
               f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
               f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best")

    file_id = uuid.uuid4().hex
    ydl_opts = {
        "outtmpl": str(DOWNLOAD_DIR / f"{file_id}.%(ext)s"),
        "format": fmt,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "concurrent_fragment_downloads": 8,
        "ffmpeg_location": FFMPEG,
        "cookiefile": COOKIEFILE,
        **extractor_args(),
        **proxy_args(),
        "progress_hooks": [dl_hook(job, "Downloading video", parts=2)],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        return jsonify(status="error", message=f"Download failed: {friendly_error(e)}"), 500
    except Exception as e:
        return jsonify(status="error", message=f"Unexpected error: {friendly_error(e)}"), 500

    produced = [
        p for p in DOWNLOAD_DIR.glob(f"{file_id}.*")
        if p.suffix.lower() in VIDEO_EXTS
    ]
    if not produced:
        return jsonify(status="error", message="No video file was produced."), 500

    out = max(produced, key=lambda p: p.stat().st_size)
    title = info.get("title", "video")

    if trimmed:
        clip = DOWNLOAD_DIR / f"{uuid.uuid4().hex}.mp4"
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(out), "-t", f"{end - start:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(clip),
        ]
        try:
            rc, errtext = run_ffmpeg(cmd, job, end - start, stage="Cutting clip")
        except FileNotFoundError:
            return jsonify(status="error", message="ffmpeg isn't installed on the server."), 500
        except subprocess.TimeoutExpired:
            return jsonify(status="error", message="Trimming took too long — try a shorter clip."), 500
        if rc != 0 or not clip.exists():
            return jsonify(status="error", message=f"Trim failed: {errtext.strip()[-150:]}"), 500
        try:
            out.unlink()          # the full download was only a means to the clip
        except OSError:
            pass
        out = clip
        title = f"{title} ({stamp(start)}-{stamp(end)})"

    TITLES[out.name] = title
    cleanup_downloads(keep={out.name})
    return jsonify(
        status="success",
        file_path=out.name,
        size_mb=round(out.stat().st_size / (1024 * 1024), 1),
        height=info.get("height") or 0,
    )


@app.route("/prepare", methods=["POST"])
def prepare():
    """Fetch a small low-quality copy purely so the browser can preview and
    trim it — never the full-quality audio.

    Pulling bestaudio here would mean a full download before you've even
    picked a range: an 11-hour meditation track is ~600 MB at its real
    bitrate. worstaudio is a fraction of that and only has to be good enough
    to recognise where you are. The real quality is fetched fresh in /cut,
    only for the range you actually choose.
    """
    job = job_from_request()
    if not code_ok():
        return jsonify(status="error", message="Wrong access code."), 403

    url = (request.form.get("url") or "").strip()
    if not YOUTUBE_RE.match(url):
        return jsonify(status="error", message="That doesn't look like a YouTube link."), 400

    file_id = uuid.uuid4().hex
    opts = {
        "outtmpl": str(DOWNLOAD_DIR / f"{file_id}_apreview.%(ext)s"),
        "format": "worstaudio[ext=webm]/worstaudio/worst",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "concurrent_fragment_downloads": 8,
        "ffmpeg_location": FFMPEG,
        "cookiefile": COOKIEFILE,
        **extractor_args(),
        **proxy_args(),
        "progress_hooks": [dl_hook(job, "Fetching preview")],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        return jsonify(status="error", message=f"Download failed: {friendly_error(e)}"), 500
    except Exception as e:
        return jsonify(status="error", message=f"Unexpected error: {friendly_error(e)}"), 500

    produced = [
        p for p in DOWNLOAD_DIR.glob(f"{file_id}_apreview.*")
        if p.suffix.lower() in AUDIO_EXTS
    ]
    if not produced:
        return jsonify(status="error", message="No preview file was produced."), 500
    src = max(produced, key=lambda p: p.stat().st_size)

    # Play the MP3 copy if we could make one; fall back to the raw source.
    preview = make_preview(src, file_id, job, int(info.get("duration") or 0))
    if preview:
        src.unlink(missing_ok=True)
        src = preview
    cleanup_downloads(keep={src.name})
    return jsonify(
        status="success",
        stream_url=f"/stream/{src.name}",
        duration=int(info.get("duration") or 0),
        size_mb=round(src.stat().st_size / (1024 * 1024), 1),
    )


@app.route("/stream/<path:filename>")
def stream(filename):
    """Serve a prepared file inline for the player (supports seeking)."""
    name = os.path.basename(filename)
    target = DOWNLOAD_DIR / name
    ext = target.suffix.lower()
    is_video = "_vpreview" in name
    allowed = VIDEO_EXTS if is_video else AUDIO_EXTS
    if not target.exists() or ext not in allowed:
        return "File not found", 404
    # .webm can hold either, so the name decides which content type to claim.
    mime = ("video/webm" if ext == ".webm" else "video/mp4") if is_video else MIME_BY_EXT.get(ext)
    return send_file(target, mimetype=mime, conditional=True)


@app.route("/cut", methods=["POST"])
def cut():
    """Fetch the real-quality audio and hand back just the selected range.

    The preview never held full quality, so the actual source is fetched here
    — only once, only for the range and quality you actually chose.
    """
    job = job_from_request()
    if not code_ok():
        return jsonify(status="error", message="Wrong access code."), 403

    url = (request.form.get("url") or "").strip()
    if not YOUTUBE_RE.match(url):
        return jsonify(status="error", message="That doesn't look like a YouTube link."), 400

    try:
        start = max(0.0, float(request.form.get("start") or 0))
        end = float(request.form.get("end") or 0)
    except ValueError:
        return jsonify(status="error", message="Those trim points aren't valid."), 400
    if end - start < 0.1:
        return jsonify(status="error", message="The end must come after the start."), 400

    file_id = uuid.uuid4().hex
    fetch_opts = {
        "outtmpl": str(DOWNLOAD_DIR / f"{file_id}.%(ext)s"),
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "concurrent_fragment_downloads": 8,
        "ffmpeg_location": FFMPEG,
        "cookiefile": COOKIEFILE,
        **extractor_args(),
        **proxy_args(),
        "progress_hooks": [dl_hook(job, "Fetching audio")],
    }
    try:
        with yt_dlp.YoutubeDL(fetch_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        return jsonify(status="error", message=f"Download failed: {friendly_error(e)}"), 500
    except Exception as e:
        return jsonify(status="error", message=f"Unexpected error: {friendly_error(e)}"), 500

    src = find_source(file_id)
    if not src:
        return jsonify(status="error", message="No audio file was produced."), 500

    choice = (request.form.get("format") or "mp3_192").strip()
    out_id = uuid.uuid4().hex
    if choice.startswith("mp3_"):
        kbps = choice.split("_", 1)[1]
        if kbps not in ("128", "192", "320"):
            kbps = "192"
        out = DOWNLOAD_DIR / f"{out_id}.mp3"
        codec = ["-c:a", "libmp3lame", "-b:a", f"{kbps}k"]
    else:  # original: copy the source stream, no re-encode
        out = DOWNLOAD_DIR / f"{out_id}{src.suffix}"
        codec = ["-c:a", "copy"]

    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}",
        "-vn", *codec, str(out),
    ]
    try:
        rc, errtext = run_ffmpeg(cmd, job, end - start, stage="Converting audio")
    except FileNotFoundError:
        return jsonify(status="error", message="ffmpeg isn't installed on the server."), 500
    except subprocess.TimeoutExpired:
        return jsonify(status="error", message="Trimming took too long — try a shorter clip."), 500
    if rc != 0 or not out.exists():
        return jsonify(status="error", message=f"Trim failed: {errtext.strip()[-150:]}"), 500

    try:
        src.unlink()          # the full fetch was only a means to the clip
    except OSError:
        pass

    title = info.get("title", "audio")
    if (request.form.get("trimmed") or "") == "1":
        title = f"{title} ({stamp(start)}-{stamp(end)})"
    TITLES[out.name] = title
    cleanup_downloads(keep={out.name})
    return jsonify(status="success", file_path=out.name)


@app.route("/download", methods=["POST"])
def download():
    if not code_ok():
        return jsonify(status="error", message="Wrong access code."), 403

    url = (request.form.get("url") or "").strip()
    choice = (request.form.get("format") or "mp3_192").strip()
    if not YOUTUBE_RE.match(url):
        return jsonify(status="error", message="That doesn't look like a YouTube link."), 400

    file_id = uuid.uuid4().hex
    ydl_opts = {
        "outtmpl": str(DOWNLOAD_DIR / f"{file_id}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "concurrent_fragment_downloads": 8,
        "ffmpeg_location": FFMPEG,
        "cookiefile": COOKIEFILE,
        **extractor_args(),
        **proxy_args(),
    }

    if choice.startswith("mp3_"):
        kbps = choice.split("_", 1)[1]
        if kbps not in ("128", "192", "320"):
            kbps = "192"
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": kbps}
        ]
    else:  # original: no re-encode, keep source container
        ydl_opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        return jsonify(status="error", message=f"Download failed: {friendly_error(e)}"), 500
    except Exception as e:
        return jsonify(status="error", message=f"Unexpected error: {friendly_error(e)}"), 500

    produced = list(DOWNLOAD_DIR.glob(f"{file_id}.*"))
    if choice.startswith("mp3_"):
        produced = [p for p in produced if p.suffix == ".mp3"] or produced
    if not produced:
        return jsonify(status="error", message="Conversion failed — no output file."), 500

    out = produced[0]
    TITLES[out.name] = info.get("title", "audio")
    cleanup_downloads()
    return jsonify(status="success", file_path=out.name)


@app.route("/get-file/<path:filename>")
def get_file(filename):
    # Only serve a bare audio filename from downloads/ (guards path traversal).
    name = os.path.basename(filename)
    target = DOWNLOAD_DIR / name
    if not target.exists() or target.suffix.lower() not in SERVABLE_EXTS:
        return "File not found", 404
    title = TITLES.get(name, "audio")
    nice = re.sub(r'[\\/:*?"<>|]', "_", title) + target.suffix
    return send_file(target, as_attachment=True, download_name=nice)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Audio Downloader running at  http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
