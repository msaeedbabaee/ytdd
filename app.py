"""
YouTube Downloader — Streamlit UI around the official yt-dlp binary.

Works with:
- a single video link
- a single playlist link (with item-range selection)
- many different links pasted together (mix of single videos / playlists)

Works both:
- locally (files also land in ./downloads/<job>/ on your machine)
- on Streamlit Community Cloud (no persistent/accessible local disk for the
  visitor, so results are also offered as an in-browser download — a ZIP
  for multiple files, or the file itself when there is only one)

See README.md for deployment notes (packages.txt installs FFmpeg on Cloud).
"""

import io
import json
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

import streamlit as st

# --------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
BIN_DIR = APP_DIR / "bin"
DOWNLOADS_DIR = APP_DIR / "downloads"
BIN_DIR.mkdir(exist_ok=True)
DOWNLOADS_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = APP_DIR / "settings.json"

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
YTDLP_WIN_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
YTDLP_LATEST_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"

# Disk-space guardrail: Streamlit Community Cloud gives each app a small,
# ephemeral disk. Warn (rather than silently filling the disk) past this.
CLOUD_SAFE_SIZE_HINT_MB = 800

DEFAULT_SETTINGS = {
    "quality": "Best",
    "audio_format": "mp3",
    "naming": "شماره - عنوان",
    "embed_thumbnail": False,
    "write_subs": False,
    "sub_langs": "fa,en",
    "concurrent_fragments": 1,
    "alt_client": False,
}

# In-memory registry of background jobs. Keyed by a random job id so the
# Streamlit session only needs to remember that id (a plain string, safe to
# keep in st.session_state); the Popen object / log lines / counters live
# here and are mutated by the worker thread.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

st.set_page_config(page_title="YouTube Downloader", page_icon="⬇️", layout="centered")

# --------------------------------------------------------------------------
# Settings persistence (best-effort: on Cloud this resets on redeploy/restart)
# --------------------------------------------------------------------------


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return settings


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_FILE.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


# --------------------------------------------------------------------------
# yt-dlp binary management
# --------------------------------------------------------------------------


def ytdlp_path() -> Path:
    if platform.system().lower().startswith("win"):
        return BIN_DIR / "yt-dlp.exe"
    return BIN_DIR / "yt-dlp"


def _download_ytdlp(path: Path) -> None:
    url = YTDLP_WIN_URL if platform.system().lower().startswith("win") else YTDLP_URL
    req = Request(url, headers={"User-Agent": "yt-downloader/3.0"})
    with urlopen(req, timeout=60) as response:
        data = response.read()
    path.write_bytes(data)
    if not platform.system().lower().startswith("win"):
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def ensure_ytdlp() -> Path:
    path = ytdlp_path()
    if not path.exists():
        _download_ytdlp(path)
    return path


def local_ytdlp_version() -> str | None:
    path = ytdlp_path()
    if not path.exists():
        return None
    try:
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=20
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def remote_ytdlp_version() -> str | None:
    try:
        req = Request(YTDLP_LATEST_API, headers={"User-Agent": "yt-downloader/3.0"})
        with urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("tag_name")
    except Exception:
        return None


def deno_path() -> Path:
    if platform.system().lower().startswith("win"):
        return BIN_DIR / "deno.exe"
    return BIN_DIR / "deno"


def _deno_asset() -> str:
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sysname.startswith("win"):
        return "deno-x86_64-pc-windows-msvc.zip"
    if sysname == "darwin":
        return "deno-aarch64-apple-darwin.zip" if arm else "deno-x86_64-apple-darwin.zip"
    return "deno-aarch64-unknown-linux-gnu.zip" if arm else "deno-x86_64-unknown-linux-gnu.zip"


def ensure_deno() -> Path | None:
    """yt-dlp needs a JS runtime for YouTube. Fetch the portable Deno binary."""
    path = deno_path()
    if path.exists():
        return path
    try:
        url = f"https://github.com/denoland/deno/releases/latest/download/{_deno_asset()}"
        req = Request(url, headers={"User-Agent": "yt-downloader/4.0"})
        with urlopen(req, timeout=120) as response:
            data = response.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = next(n for n in zf.namelist() if n.split("/")[-1].startswith("deno"))
            path.write_bytes(zf.read(member))
        if not platform.system().lower().startswith("win"):
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        return None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffmpeg_hint() -> str:
    sysname = platform.system().lower()
    if sysname.startswith("win"):
        return "winget install ffmpeg  یا دانلود از ffmpeg.org و افزودن به PATH"
    if sysname == "darwin":
        return "brew install ffmpeg"
    return "sudo apt install ffmpeg   (روی Streamlit Cloud کافی است ffmpeg را در packages.txt بگذارید)"


# --------------------------------------------------------------------------
# Link parsing / validation
# --------------------------------------------------------------------------


def parse_links(raw_text: str) -> tuple[list[str], list[str]]:
    """Split free-form pasted text into a list of valid/invalid YouTube links."""
    candidates = re.split(r"[\s,]+", raw_text.strip())
    valid, invalid = [], []
    seen = set()
    for c in candidates:
        c = c.strip()
        if not c:
            continue
        if c in seen:
            continue
        seen.add(c)
        if "youtube.com" in c or "youtu.be" in c:
            valid.append(c)
        else:
            invalid.append(c)
    return valid, invalid


# --------------------------------------------------------------------------
# Playlist / video preview
# --------------------------------------------------------------------------


def fetch_link_info(exe: Path, url: str) -> dict:
    cmd = [str(exe), "--flat-playlist", "--dump-single-json", "--no-warnings", url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "خطای نامشخص در دریافت اطلاعات لینک")
    data = json.loads(result.stdout)
    entries = data.get("entries") or [data]
    items = []
    for i, e in enumerate(entries, start=1):
        if not e:
            continue
        items.append({"index": i, "title": e.get("title") or e.get("id") or f"آیتم {i}"})
    return {
        "url": url,
        "title": data.get("title") or "ویدئوی تکی",
        "uploader": data.get("uploader") or data.get("channel"),
        "is_playlist": bool(data.get("entries")),
        "items": items,
    }


# --------------------------------------------------------------------------
# Download command construction
# --------------------------------------------------------------------------


def quality_format(quality: str) -> str:
    return {
        "Best": "bv*+ba/b",
        "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
        "720p": "bv*[height<=720]+ba/b[height<=720]",
        "480p": "bv*[height<=480]+ba/b[height<=480]",
        "Audio only": "ba/b",
    }.get(quality, "bv*+ba/b")


def naming_template(naming: str) -> str:
    # playlist_autonumber (unlike playlist_index) is always defined — it's
    # "1" for a standalone video too — so filenames never show "NA".
    return {
        "شماره - عنوان": "%(playlist_autonumber)03d - %(title)s.%(ext)s",
        "عنوان": "%(title)s.%(ext)s",
        "کانال - عنوان": "%(uploader)s - %(title)s.%(ext)s",
    }.get(naming, "%(playlist_autonumber)03d - %(title)s.%(ext)s")


def build_command(exe: Path, url: str, output_dir: Path, opts: dict) -> list:
    output_template = str(output_dir / naming_template(opts["naming"]))
    cmd = [
        str(exe),
        "--newline",
        "--ignore-errors",
        "--continue",
        "--no-overwrites",
        "--yes-playlist",
        "--progress",
        "--progress-template",
        "%(progress._percent_str)s | %(progress._speed_str)s | %(progress._eta_str)s | %(progress.filename)s",
        "-f",
        quality_format(opts["quality"]),
        "-o",
        output_template,
        "--concurrent-fragments",
        str(max(1, min(8, int(opts.get("concurrent_fragments", 1))))),
    ]
    if opts.get("playlist_items"):
        cmd += ["--playlist-items", opts["playlist_items"]]
    if opts["quality"] == "Audio only":
        cmd += ["-x", "--audio-format", opts.get("audio_format", "mp3")]
    if opts.get("embed_thumbnail"):
        cmd += ["--embed-thumbnail"]
    if deno_path().exists():
        cmd += ["--js-runtimes", f"deno:{deno_path()}"]
    if opts.get("cookies_path"):
        cmd += ["--cookies", opts["cookies_path"]]
    if opts.get("alt_client"):
        cmd += ["--extractor-args", "youtube:player_client=tv,web_safari,default"]
    if opts.get("write_subs"):
        cmd += [
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            opts.get("sub_langs", "en"),
            "--embed-subs",
        ]
    cmd.append(url)
    return cmd


# --------------------------------------------------------------------------
# Background worker — processes a batch of links sequentially
# --------------------------------------------------------------------------

PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
ITEM_RE = re.compile(r"Downloading item (\d+) of (\d+)")


def _run_one(exe, url, output_dir, opts, job) -> int:
    creationflags = 0
    if platform.system().lower().startswith("win"):
        creationflags = subprocess.CREATE_NO_WINDOW

    cmd = build_command(exe, url, output_dir, opts)
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        creationflags=creationflags,
    )
    with JOBS_LOCK:
        job["process"] = process

    current_file = ""
    for raw in process.stdout:
        line = raw.rstrip()
        if not line:
            continue
        with JOBS_LOCK:
            job["log_lines"].append(line)
            job["log_lines"] = job["log_lines"][-150:]

            m_item = ITEM_RE.search(line)
            if m_item:
                job["current_item"] = int(m_item.group(1))
                job["total_items"] = int(m_item.group(2))

            fname = None
            if "[download] Destination:" in line:
                fname = line.split("[download] Destination:", 1)[1].strip()
            elif "Merger] Merging formats into" in line and '"' in line:
                fname = line.split('"')[1]
            elif "has already been downloaded" in line and "[download] " in line:
                # Format: "[download] /path/to/file.ext has already been downloaded"
                fname = line.split("[download] ", 1)[1].rsplit(
                    " has already been downloaded", 1
                )[0].strip()
                job["skipped"] += 1

            if fname:
                current_file = fname
                if fname not in job["downloaded_files"]:
                    job["downloaded_files"].append(fname)

            if line.startswith("ERROR:"):
                job["failed"] += 1
                if "sign in" in line.lower() or "not a bot" in line.lower():
                    job["bot_check_hit"] = True

            m_pct = PCT_RE.search(line)
            if m_pct:
                pct = float(m_pct.group(1))
                job["current_pct"] = pct
                job["current_file"] = current_file
                total = job["total_items"] or 1
                idx = max(job["current_item"] - 1, 0)
                job["link_pct"] = min(100.0, ((idx + pct / 100.0) / total) * 100.0)

            if job.get("cancel_requested"):
                break

    return process.wait()


def _zip_results(job_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in job_dir.rglob("*"):
            if f.is_file() and f.suffix != ".zip":
                zf.write(f, arcname=str(f.relative_to(job_dir)))


def _worker(job_id: str, urls: list, opts: dict) -> None:
    job = JOBS[job_id]
    try:
        exe = ensure_ytdlp()
    except Exception as exc:
        with JOBS_LOCK:
            job["status"] = "error"
            job["error_message"] = f"دانلود باینری yt-dlp ناموفق بود: {exc}"
        return

    ensure_deno()  # non-fatal; needed by yt-dlp for YouTube JS challenges

    job_dir = DOWNLOADS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    multi = len(urls) > 1

    for i, url in enumerate(urls, start=1):
        with JOBS_LOCK:
            if job.get("cancel_requested"):
                break
            job["current_url_index"] = i
            job["current_item"] = 0
            job["total_items"] = 0
            job["link_pct"] = 0.0

        link_dir = job_dir / f"{i:02d}" if multi else job_dir
        link_dir.mkdir(parents=True, exist_ok=True)

        try:
            code = _run_one(exe, url, link_dir, opts, job)
        except Exception as exc:
            with JOBS_LOCK:
                job["failed"] += 1
                job["log_lines"].append(f"ERROR: اجرای yt-dlp برای {url} ناموفق بود: {exc}")
            continue

        with JOBS_LOCK:
            if code != 0 and not job.get("cancel_requested"):
                job["failed_links"].append(url)

    with JOBS_LOCK:
        files = job["downloaded_files"]
        if job.get("cancel_requested"):
            job["status"] = "canceled"
        elif job["failed"] or job["failed_links"]:
            job["status"] = "done_with_errors"
        else:
            job["status"] = "done"
        job["batch_pct"] = 100.0
        job["end_time"] = time.time()

    # Package results for in-browser download (works locally and on Cloud).
    try:
        if len(files) == 1:
            job["single_file"] = files[0]
        elif len(files) > 1:
            zip_path = DOWNLOADS_DIR / f"{job_id}.zip"
            _zip_results(job_dir, zip_path)
            job["zip_path"] = str(zip_path)
    except Exception as exc:
        job["log_lines"].append(f"ERROR: آماده‌سازی خروجی برای دانلود ناموفق بود: {exc}")


def start_job(urls: list, opts: dict) -> str:
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "status": "running",
        "urls": urls,
        "current_url_index": 0,
        "total_urls": len(urls),
        "log_lines": [],
        "current_item": 0,
        "total_items": 0,
        "current_pct": 0.0,
        "link_pct": 0.0,
        "batch_pct": 0.0,
        "current_file": "",
        "downloaded_files": [],
        "failed_links": [],
        "skipped": 0,
        "failed": 0,
        "cancel_requested": False,
        "bot_check_hit": False,
        "process": None,
        "single_file": None,
        "zip_path": None,
        "start_time": time.time(),
        "end_time": None,
        "error_message": None,
    }
    thread = threading.Thread(target=_worker, args=(job_id, urls, opts), daemon=True)
    thread.start()
    return job_id


def cancel_job(job_id: str) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    with JOBS_LOCK:
        job["cancel_requested"] = True
        proc = job.get("process")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass


def cleanup_job(job_id: str) -> None:
    job_dir = DOWNLOADS_DIR / job_id
    zip_path = DOWNLOADS_DIR / f"{job_id}.zip"
    shutil.rmtree(job_dir, ignore_errors=True)
    zip_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

settings = load_settings()

st.title("⬇️ YouTube Downloader")
st.caption(
    "دانلود یک ویدئو، یک پلی‌لیست، یا چند لینک متفاوت — با yt-dlp. "
    "نتیجه هم به‌صورت فایل قابل‌دانلود در مرورگر داده می‌شود، هم (در اجرای محلی) روی دیسک ذخیره می‌شود. "
    "فقط برای محتوایی استفاده کنید که حق دانلود آن را دارید."
)

with st.sidebar:
    st.header("تنظیمات")

    settings["quality"] = st.selectbox(
        "کیفیت",
        ["Best", "1080p", "720p", "480p", "Audio only"],
        index=["Best", "1080p", "720p", "480p", "Audio only"].index(settings["quality"]),
    )
    if settings["quality"] == "Audio only":
        settings["audio_format"] = st.selectbox(
            "فرمت صدا",
            ["mp3", "m4a", "opus", "wav"],
            index=["mp3", "m4a", "opus", "wav"].index(settings.get("audio_format", "mp3")),
        )

    settings["naming"] = st.selectbox(
        "الگوی نام‌گذاری فایل",
        ["شماره - عنوان", "عنوان", "کانال - عنوان"],
        index=["شماره - عنوان", "عنوان", "کانال - عنوان"].index(settings["naming"]),
    )

    with st.expander("گزینه‌های پیشرفته"):
        settings["embed_thumbnail"] = st.checkbox(
            "افزودن تصویر بندانگشتی (نیاز به FFmpeg)",
            value=settings.get("embed_thumbnail", False),
        )
        settings["write_subs"] = st.checkbox(
            "دانلود و جاسازی زیرنویس", value=settings.get("write_subs", False)
        )
        if settings["write_subs"]:
            settings["sub_langs"] = st.text_input(
                "زبان زیرنویس (کد زبان، جدا با کاما)",
                value=settings.get("sub_langs", "fa,en"),
            )
        settings["concurrent_fragments"] = st.slider(
            "تعداد قطعات موازی (سرعت)",
            min_value=1,
            max_value=8,
            value=int(settings.get("concurrent_fragments", 1)),
        )
        settings["alt_client"] = st.checkbox(
            "روش سازگاری بیشتر با یوتیوب (اگر خطای «Sign in to confirm you're not "
            "a bot» یا دانلود صفر می‌گیرید، این را فعال کنید)",
            value=settings.get("alt_client", False),
        )

    with st.expander("کوکی YouTube (در صورت نیاز)"):
        st.caption(
            "اگر خطای «Sign in to confirm you're not a bot» می‌گیرید یا پلی‌لیست/"
            "ویدئوی خصوصی دارید، فایل کوکی مرورگر خود (فرمت Netscape، مثلاً با افزونهٔ "
            "'Get cookies.txt') را این‌جا بارگذاری کنید. از یک حساب فرعی استفاده کنید، "
            "نه حساب اصلی‌تان."
        )
        cookies_upload = st.file_uploader("فایل cookies.txt", type=["txt"])
        cookies_path = None
        if cookies_upload is not None:
            cookies_path = str(APP_DIR / "cookies_upload.txt")
            Path(cookies_path).write_bytes(cookies_upload.getvalue())
            st.success("کوکی بارگذاری شد و در این اجرا استفاده می‌شود.")

    save_settings({k: v for k, v in settings.items()})

    st.divider()
    st.subheader("وضعیت ابزارها")
    if ffmpeg_available():
        st.success("FFmpeg نصب است ✅")
    else:
        st.warning(f"FFmpeg پیدا نشد.\n`{ffmpeg_hint()}`")

    if deno_path().exists():
        st.success("Deno (موتور JS برای YouTube) آماده است ✅")
    else:
        st.info("Deno در اولین دانلود به‌صورت خودکار دریافت می‌شود.")

    if st.button("🔄 بررسی / بروزرسانی yt-dlp", use_container_width=True):
        with st.spinner("در حال بررسی..."):
            local_v = local_ytdlp_version()
            remote_v = remote_ytdlp_version()
            try:
                if local_v is None:
                    ensure_ytdlp()
                    st.success(f"yt-dlp نصب شد — نسخه {local_ytdlp_version()}")
                elif remote_v and remote_v != local_v:
                    _download_ytdlp(ytdlp_path())
                    st.success(f"به‌روزرسانی شد: {local_v} → {local_ytdlp_version()}")
                else:
                    st.info(f"yt-dlp به‌روز است — نسخه {local_v}")
            except Exception as exc:
                st.error(f"خطا: {exc}")

# --- main area -------------------------------------------------------------

if "job_id" not in st.session_state:
    st.session_state.job_id = None
if "preview" not in st.session_state:
    st.session_state.preview = None

raw_links = st.text_area(
    "یک یا چند لینک YouTube (هر خط یا هرکدام جدا با کاما/فاصله — ویدئوی تکی یا پلی‌لیست، مخلوط هم می‌شود)",
    height=120,
    placeholder="https://www.youtube.com/watch?v=...\nhttps://www.youtube.com/playlist?list=...\nhttps://youtu.be/...",
)

job_running = (
    st.session_state.job_id is not None
    and JOBS.get(st.session_state.job_id, {}).get("status") == "running"
)

col1, col2 = st.columns(2)
with col1:
    preview_clicked = st.button("🔎 پیش‌نمایش", use_container_width=True, disabled=job_running)
with col2:
    clear_clicked = st.button("🧹 پاک کردن نتیجه", use_container_width=True)

if clear_clicked:
    if st.session_state.job_id:
        cleanup_job(st.session_state.job_id)
    st.session_state.job_id = None
    st.session_state.preview = None
    st.rerun()

if preview_clicked:
    valid, invalid = parse_links(raw_links)
    if not valid:
        st.warning("هیچ لینک معتبر YouTube پیدا نشد.")
        st.session_state.preview = None
    else:
        if invalid:
            st.warning(f"{len(invalid)} مورد نامعتبر نادیده گرفته شد: {', '.join(invalid[:5])}")
        preview_limit = 10
        infos = []
        errors = []
        try:
            exe = ensure_ytdlp()
        except Exception as exc:
            st.error(f"آماده‌سازی yt-dlp ناموفق بود: {exc}")
            exe = None
        if exe:
            with st.spinner(f"در حال دریافت اطلاعات {min(len(valid), preview_limit)} لینک..."):
                for u in valid[:preview_limit]:
                    try:
                        infos.append(fetch_link_info(exe, u))
                    except Exception as exc:
                        errors.append((u, str(exc)))
            st.session_state.preview = {
                "valid_urls": valid,
                "infos": infos,
                "errors": errors,
                "truncated": len(valid) > preview_limit,
            }

preview = st.session_state.preview
playlist_items_spec = ""

if preview:
    valid_urls = preview["valid_urls"]
    infos = preview["infos"]
    errors = preview["errors"]

    st.write(f"تعداد لینک‌های معتبر: **{len(valid_urls)}**")
    for info in infos:
        with st.expander(f"{info['title']}" + (f" — {info['uploader']}" if info.get("uploader") else "")):
            if info["is_playlist"]:
                st.caption(f"{len(info['items'])} آیتم در این پلی‌لیست")
                for it in info["items"][:100]:
                    st.write(f"{it['index']}. {it['title']}")
                if len(info["items"]) > 100:
                    st.caption(f"... و {len(info['items']) - 100} مورد دیگر")
            else:
                st.caption("ویدئوی تکی")

    for u, err in errors:
        st.error(f"خطا در دریافت اطلاعات `{u}`: {err}")

    if preview["truncated"]:
        st.caption("برای سرعت بیشتر، پیش‌نمایش فقط ۱۰ لینک اول را نشان می‌دهد؛ همه لینک‌ها هنگام دانلود پردازش می‌شوند.")

    # Per-item selection only makes sense for exactly one playlist link.
    if len(valid_urls) == 1 and infos and infos[0]["is_playlist"]:
        playlist_items_spec = st.text_input(
            "کدام آیتم‌ها دانلود شوند؟ (خالی = همه)",
            placeholder=f"مثال: 1-5,8,10-{len(infos[0]['items'])}",
            help="سینتکس yt-dlp: بازه با خط تیره، موارد جدا با کاما.",
        )

start_disabled = job_running
start = st.button("▶️ شروع دانلود", type="primary", use_container_width=True, disabled=start_disabled)

if start:
    valid, invalid = parse_links(raw_links)
    if not valid:
        st.warning("ابتدا حداقل یک لینک معتبر YouTube وارد کنید.")
    else:
        if invalid:
            st.warning(f"{len(invalid)} مورد نامعتبر نادیده گرفته شد: {', '.join(invalid[:5])}")
        opts = dict(settings)
        opts["playlist_items"] = (
            (playlist_items_spec.strip() or None) if len(valid) == 1 else None
        )
        opts["cookies_path"] = cookies_path
        job_id = start_job(valid, opts)
        st.session_state.job_id = job_id
        st.rerun()

# --- live progress / result -------------------------------------------------

job_id = st.session_state.job_id
if job_id and job_id in JOBS:
    job = JOBS[job_id]
    status = job["status"]

    if status == "running":
        total_urls = job["total_urls"]
        if total_urls > 1:
            st.info(f"لینک {job['current_url_index']} از {total_urls}")
            batch_pct = ((job["current_url_index"] - 1) + job["link_pct"] / 100.0) / total_urls * 100.0
            st.progress(int(min(100, batch_pct)))
        if job["total_items"] > 1:
            st.write(f"آیتم {job['current_item']} از {job['total_items']}")
        st.progress(int(job["link_pct"]))

        fname = Path(job["current_file"]).name if job["current_file"] else "..."
        st.write(f"**در حال دانلود:** `{fname}` — {job['current_pct']:.1f}%")

        c1, c2, c3 = st.columns(3)
        c1.metric("موفق", max(0, len(job["downloaded_files"]) - job["skipped"]))
        c2.metric("رد شده", job["skipped"])
        c3.metric("خطا", job["failed"])

        with st.expander("لاگ کامل", expanded=False):
            st.code("\n".join(job["log_lines"][-150:]), language="text")

        if st.button("⏹️ لغو دانلود", use_container_width=True):
            cancel_job(job_id)
            st.rerun()

        time.sleep(1)
        st.rerun()

    else:
        elapsed = ""
        if job.get("start_time") and job.get("end_time"):
            elapsed = f" در {job['end_time'] - job['start_time']:.0f} ثانیه"

        if status == "done":
            st.success(f"دانلود با موفقیت تمام شد{elapsed}.")
        elif status == "done_with_errors":
            st.warning("برخی موارد دانلود نشدند (خطای دسترسی، محدودیت منطقه‌ای، یا لینک نامعتبر).")
        elif status == "canceled":
            st.warning("دانلود لغو شد.")
        elif status == "error":
            st.error(job.get("error_message") or "خطای نامشخص")

        if job["downloaded_files"] or job["skipped"] or job["failed"]:
            c1, c2, c3 = st.columns(3)
            c1.metric("موفق", max(0, len(job["downloaded_files"]) - job["skipped"]))
            c2.metric("رد شده", job["skipped"])
            c3.metric("خطا", job["failed"])

        if job["failed_links"]:
            with st.expander("لینک‌هایی که کامل دانلود نشدند"):
                for u in job["failed_links"]:
                    st.write(f"❌ {u}")

        if job.get("bot_check_hit") or (job["failed"] and not job["downloaded_files"]):
            st.error(
                "به‌نظر می‌رسد YouTube درخواست را به‌عنوان ربات تشخیص داده "
                "(خطای «Sign in to confirm you're not a bot») یا هیچ فایلی ساخته نشده. "
                "این روزها روی سرورها/میزبانی‌های ابری (مثل Streamlit Cloud) خیلی رایج است. راه‌حل‌های پیشنهادی:\n\n"
                "۱. از منوی کناری روی «بررسی/بروزرسانی yt-dlp» بزنید (اغلب کافی است).\n"
                "۲. تیک «روش سازگاری بیشتر با یوتیوب» را در گزینه‌های پیشرفته فعال کنید و دوباره امتحان کنید.\n"
                "۳. یک فایل کوکی مرورگر (از یک حساب فرعی) در بخش «کوکی YouTube» بارگذاری کنید.\n"
                "۴. اگر روی Streamlit Cloud اجرا می‌کنید، همین را یک‌بار به‌صورت محلی روی سیستم خودتان امتحان کنید؛ "
                "آی‌پی سرورهای ابری بیشتر مسدود می‌شود."
            )

        # --- in-browser download (works on Streamlit Cloud too) -----------
        if job.get("single_file"):
            p = Path(job["single_file"])
            if p.exists():
                st.download_button(
                    "⬇️ دانلود فایل",
                    data=p.read_bytes(),
                    file_name=p.name,
                    use_container_width=True,
                )
        elif job.get("zip_path"):
            zp = Path(job["zip_path"])
            if zp.exists():
                size_mb = zp.stat().st_size / (1024 * 1024)
                if size_mb > CLOUD_SAFE_SIZE_HINT_MB:
                    st.caption(
                        f"حجم نتیجه {size_mb:.0f} مگابایت است؛ روی Streamlit Cloud ممکن است "
                        "دانلود/حافظه محدود باشد. اجرای محلی برای حجم‌های بزرگ مطمئن‌تر است."
                    )
                st.download_button(
                    "⬇️ دانلود همه به‌صورت ZIP",
                    data=zp.read_bytes(),
                    file_name="youtube-downloads.zip",
                    mime="application/zip",
                    use_container_width=True,
                )

        if job["downloaded_files"]:
            with st.expander("فهرست فایل‌های دانلودشده", expanded=False):
                for f in job["downloaded_files"]:
                    st.write(f"📄 {Path(f).name}")

        with st.expander("لاگ کامل", expanded=False):
            st.code("\n".join(job["log_lines"][-150:]), language="text")

st.divider()
st.caption(
    "نکات: ۱) برای ادغام ویدئو/صدا، خروجی MP3 و جاسازی زیرنویس/بندانگشتی، FFmpeg لازم است "
    "(روی Streamlit Cloud با افزودن ffmpeg به packages.txt نصب می‌شود). "
    "۲) پلی‌لیست‌های خصوصی نیاز به کوکی/احراز هویت دارند که پشتیبانی نمی‌شود. "
    "۳) روی Streamlit Cloud فضای دیسک و زمان اجرا محدود است؛ برای حجم زیاد، اجرای محلی مطمئن‌تر است. "
    "۴) فقط محتوایی را دانلود کنید که اجازهٔ آن را دارید و شرایط استفادهٔ YouTube را رعایت کنید."
)
