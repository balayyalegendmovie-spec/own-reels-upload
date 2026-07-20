#!/usr/bin/env python3
"""
============================================================
FULLY AUTOMATED INSTAGRAM REELS UPLOADER — SINGLE FILE (V2)
============================================================

MODULAR ARCHITECTURE — One file, many responsibilities separated.

SOURCE: User-curated spreadsheet/file (source_movies.csv / source.json)
        You paste URLs + titles yourself (e.g., from Mega / Drive / any
        authorized source). The automation never scrapes unauthorized sites.

NOTE: You must have legal authorization to download and process any
content referenced in your source file. This framework does not implement
site-specific scraping for copyrighted material.

Modules inside:
  - Config / Logging
  - Source (spreadsheet/file reader + new-movie filter)
  - Download Manager (retries, resume, integrity check)
  - Audio Selector (auto Telugu audio selection)
  - Video Processor (duration, clip extraction, thumbnail)
  - Caption Generator (Gemini + fallback)
  - Instagram Uploader (session login, anti-detect, retry)
  - Progress Tracker (resume-safe state + history)
  - Main pipeline

Usage:
  1. Populate source_movies.csv with columns:
     slug,url,title,quality
  2. Set environment secrets / .env
  3. Run: python main.py

All progress is saved to progress.json, movies_log.json, upload_history.json.
Restart after interruption resumes exactly at the last part.
"""

# =====================================================================
# IMPORTS
# =====================================================================
import os, sys, re, csv, json, time, random, shutil, traceback, hashlib, requests, subprocess
from pathlib import Path
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional, List, Dict, Any, Tuple

# External dependencies (install if missing: pip install pillow instagrapi google-genai)
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None  # type: ignore

try:
    from instagrapi import Client
    from instagrapi.exceptions import (
        LoginRequired, ChallengeRequired, FeedbackRequired,
        PleaseWaitFewMinutes, ClientThrottledError,
    )
except ImportError:
    Client = None  # type: ignore
    LoginRequired = ChallengeRequired = FeedbackRequired = None  # type: ignore
    PleaseWaitFewMinutes = ClientThrottledError = None  # type: ignore

try:
    from google import genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    genai = None  # type: ignore
    genai_types = None  # type: ignore

os.environ.setdefault("PYTHONUNBUFFERED", "1")

# =====================================================================
# PATCH: Fix tenacity Python 3.11+ compatibility for mega.py
# Must patch file BEFORE importing tenacity, because import fails.
# =====================================================================
patched = False
try:
    import glob, sys
    for site_path in sys.path:
        for match in glob.glob(os.path.join(site_path, "tenacity/_asyncio.py")):
            if os.path.exists(match):
                with open(match, "r", encoding="utf-8") as f:
                    content = f.read()
                if "@asyncio.coroutine" in content:
                    content = content.replace("@asyncio.coroutine", "# @asyncio.coroutine")
                    with open(match, "w", encoding="utf-8") as f:
                        f.write(content)
                    patched = True
                    # Log via print (log function not defined yet) — will confirm after
                    print(f"[PATCH] Fixed tenacity at {match} for Python 3.11+")
except Exception as exc:
    print(f"[PATCH] Warning: could not patch tenacity: {exc}")

# Now safe to import tenacity / mega after patch
try:
    import tenacity
except Exception:
    pass


# =====================================================================
# CONFIG
# =====================================================================
class C:
    # Auth / secrets (from env or .env file)
    IG_USER = os.environ.get("IG_USERNAME", "")
    IG_PASS = os.environ.get("IG_PASSWORD", "")
    IG_SESSION = os.environ.get("IG_SESSION", "")

    # Source configuration
    SOURCE_FILE = os.environ.get("SOURCE_FILE", "sources.txt")  # Line-based URLs; add ✅ when done
    SOURCE_JSON = os.environ.get("SOURCE_JSON", "source_movies.json")  # Fallback JSON list

    # Drive / external keys (optional, preserved from original)
    DRIVE_FOLDER = os.environ.get("GDRIVE_FOLDER_ID", "")
    DRIVE_KEY = os.environ.get("GDRIVE_API_KEY", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

    # Content settings
    WATERMARK = os.environ.get("WATERMARK_TEXT", "")
    LANGUAGE = os.environ.get("CONTENT_LANGUAGE", "telugu").lower()
    CLIP_LEN = int(os.environ.get("CLIP_LEN", "95"))  # Shorter clips = higher retention = faster growth

    # Pipeline limits
    MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "1"))
    MAX_ERRORS = int(os.environ.get("MAX_ERRORS", "3"))
    COOLDOWN_HRS = int(os.environ.get("COOLDOWN_HRS", "24"))

    # Visual tweaks
    ZOOM = float(os.environ.get("ZOOM", "0.03"))
    BRIGHT = float(os.environ.get("BRIGHT", "0.02"))
    CONTRAST = float(os.environ.get("CONTRAST", "1.02"))

    # Paths
    TMP = "/tmp/reelbot"
    MOVIE_FILE = f"{TMP}/movie.mp4"
    MOVIE_RAW = f"{TMP}/movie_raw.mp4"
    MOVIE_AUDIO_FIXED = f"{TMP}/movie_telugu.mp4"
    SESSION_FILE = f"{TMP}/session.json"
    CLIPS_DIR = f"{TMP}/clips"
    THUMBS_DIR = f"{TMP}/thumbs"
    FRAMES_DIR = f"{TMP}/frames"
    SOURCE_DIR = f"{TMP}/source"

    # Tracking files (persisted to workspace for GitHub resume)
    PROGRESS = "progress.json"
    LOG_FILE = "movies_log.json"
    HISTORY_FILE = "upload_history.json"

    # Fonts (optional)
    FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

    # Gemini models (ordered by preference)
    GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite"]

    # Video extensions for download validation
    VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm")


# =====================================================================
# LOGGING (never logs secrets)
# =====================================================================
SECRET_KEYS = [C.IG_PASS, C.DRIVE_KEY, C.GEMINI_KEY]
if C.IG_SESSION and len(C.IG_SESSION) > 10:
    SECRET_KEYS.append(C.IG_SESSION[:15])


def _sanitize(msg) -> str:
    s = str(msg)
    for sec in SECRET_KEYS:
        if sec and len(sec) > 5 and sec in s:
            s = s.replace(sec, "***")
    return s


def log(msg: str, prefix: str = "✅") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {prefix} {_sanitize(msg)}", flush=True)


def log_err(msg: str) -> None:
    log(msg, "❌")


def log_warn(msg: str) -> None:
    log(msg, "⚠️")


def log_step(step_num: int, total_steps: int, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] ━━━ STEP {step_num}/{total_steps}: {msg} ━━━", flush=True)


# =====================================================================
# JSON / FILE HELPERS
# =====================================================================
def load_json(fp: str, default: Any = None) -> Any:
    if default is None:
        default = {}
    try:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        log_warn(f"load_json error for {fp}: {exc}")
    return default


def save_json(fp: str, data: Any) -> None:
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        log_err(f"save_json error for {fp}: {exc}")


# =====================================================================
# GIT PUSH (subprocess only)
# =====================================================================
def git_push() -> bool:
    log("Pushing progress to GitHub...")
    try:
        subprocess.run(["git", "config", "user.name", "ReelBot"], capture_output=True, timeout=30)
        subprocess.run(["git", "config", "user.email", "bot@reelbot.com"], capture_output=True, timeout=30)

        ignore_entries = [
            "session.json", "yt_token.json", "*.mp4", "/tmp/",
            "thumb_cache/", "reels/", "thumbnails/", "tmp_frames/",
            "detailed_log.txt", "current_movie.mp4", "uploads/",
            ".env"
        ]
        existing = ""
        if os.path.exists(".gitignore"):
            existing = open(".gitignore").read()
        new_entries = [e for e in ignore_entries if e not in existing]
        if new_entries:
            with open(".gitignore", "a") as f:
                f.write("\n".join([""] + new_entries + [""]))
            subprocess.run(["git", "add", ".gitignore"], capture_output=True, timeout=30)

        for fn in [C.PROGRESS, C.LOG_FILE, C.HISTORY_FILE]:
            if os.path.exists(fn):
                subprocess.run(["git", "add", fn], capture_output=True, timeout=30)

        check = subprocess.run(["git", "diff", "--staged", "--quiet"], capture_output=True)
        if check.returncode != 0:
            subprocess.run(["git", "commit", "-m", "🤖 progress update"], capture_output=True, timeout=30)
            subprocess.run(["git", "push"], capture_output=True, timeout=60)
            log("Push complete")
        else:
            log("No changes to push")
        return True
    except Exception as exc:
        log_err(f"Git push failed: {exc}")
        return False


# =====================================================================
# SOURCE MODULE (Spreadsheet / File Reader)
# =====================================================================
class MovieItem:
    def __init__(self, slug: str, url: str, title: str = "", quality: str = "unknown"):
        self.slug = slug
        self.url = url
        self.title = title or slug
        self.quality = quality

    def to_dict(self) -> Dict[str, Any]:
        return {"slug": self.slug, "url": self.url, "title": self.title, "quality": self.quality}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MovieItem":
        return cls(
            slug=d.get("slug", d.get("id", "")),
            url=d.get("url", d.get("download_url", "")),
            title=d.get("title", d.get("name", d.get("slug", ""))),
            quality=d.get("quality", d.get("resolution", "unknown")),
        )


class SourceProvider:
    """Abstract interface: any source (file, spreadsheet, authorized API) can plug in."""

    def scan_movies(self) -> List[MovieItem]:
        raise NotImplementedError

    def filter_new_movies(self, movies: List[MovieItem], history: Dict[str, Any]) -> List[MovieItem]:
        uploaded_items = history.get("uploaded", [])
        uploaded_slugs = set()
        for item in uploaded_items:
            if isinstance(item, dict):
                sid = item.get("slug", "")
                if sid:
                    uploaded_slugs.add(str(sid))
            else:
                uploaded_slugs.add(str(item))
        processed_slugs = set()
        # Also check movies_log.json for completed movie IDs/slugs
        log_data = load_json(C.LOG_FILE, {"videos": {}, "order": []})
        for info in log_data.get("videos", {}).values():
            sid = info.get("slug") or info.get("id") or info.get("movie_slug")
            if sid:
                processed_slugs.add(str(sid))
        # Combine
        excluded = uploaded_slugs.union(processed_slugs)
        new_items = [m for m in movies if m.slug not in excluded]
        if new_items:
            log(f"Source: found {len(new_items)} new from {len(movies)} total (excluded: {len(excluded)})")
        else:
            log("Source: no new movies found")
        return new_items


# =====================================================================
# DRIVE CSV DOWNLOAD (user-curated spreadsheet from Drive folder)
# =====================================================================
def download_source_from_drive():
    """Download source_movies.csv from the configured Google Drive folder."""
    if not C.DRIVE_FOLDER or not C.DRIVE_KEY:
        return False
    log("Checking Drive for source_movies.csv...")
    try:
        url = "https://www.googleapis.com/drive/v3/files"
        params = {
            "q": f"'{C.DRIVE_FOLDER}' in parents and name='source_movies.csv' and trashed=false",
            "key": C.DRIVE_KEY,
            "fields": "files(id,name)",
            "pageSize": 10,
        }
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            log_warn(f"Drive list failed: {r.status_code}")
            return False
        data = r.json()
        files = data.get("files", [])
        if not files:
            log_warn("No source_movies.csv found in Drive folder — using local file if present")
            return False
        file_id = files[0]["id"]
        download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&key={C.DRIVE_KEY}"
        resp = requests.get(download_url, timeout=60)
        if resp.status_code == 200:
            with open(C.SOURCE_FILE, "wb") as f:
                f.write(resp.content)
            log(f"Downloaded source_movies.csv from Drive ({len(resp.content)} bytes)")
            return True
        else:
            log_warn(f"Drive download HTTP {resp.status_code}")
            return False
    except Exception as exc:
        log_warn(f"Drive download error: {exc}")
        return False


def extract_title_from_path(path: str) -> str:
    """Derive a clean display title from the downloaded file name (no HTML scraping)."""
    name = os.path.basename(path)
    # Remove common extensions and resolution tags for cleaner display
    clean = re.sub(r"\.(mp4|mkv|avi|mov|webm)$", "", name, flags=re.IGNORECASE)
    clean = re.sub(r"\[\d+p\]", "", clean)
    clean = re.sub(r"\[Multi-Audio\]", "", clean)
    clean = clean.replace("_", " ").replace("-", " ")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:60] if clean else name


class SpreadsheetSource(SourceProvider):
    """
    Reads user-curated source file.
    Expected CSV columns: slug,url,title,quality
    Example:
        doraemon_m09,doraemon_m09,Movie 09 - Nobita's Parallel Journey,1080p
    You manage this file yourself (e.g., export from spreadsheet, paste URLs).
    """

    def __init__(self):
        self.file_path = C.SOURCE_FILE

    def scan_movies(self) -> List[MovieItem]:
        # Try to pull latest source file from Drive first (if configured)
        if C.DRIVE_FOLDER and C.DRIVE_KEY:
            download_source_from_drive()
        log_step(1, 9, "Scan source file")
        movies: List[MovieItem] = []
        # If using line-based sources.txt (URL only; add ✅ when done)
        if self.file_path.endswith(".txt"):
            try:
                if os.path.exists(self.file_path):
                    with open(self.file_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith("#"):
                                continue
                            # Skip completed URLs
                            if line.endswith("✅"):
                                continue
                            url = line.split()[0] if line.split() else line
                            slug = url.split("/")[-1].split("?")[0] if url else f"movie_{len(movies)+1}"
                            if url:
                                movies.append(MovieItem(
                                    slug=slug,
                                    url=url,
                                    title=slug,
                                    quality="1080p",
                                ))
            except Exception as exc:
                log_warn(f"Failed to read text source: {exc}")
            log(f"Source loaded: {len(movies)} entries from {self.file_path}")
            for m in movies:
                log(f"  {m.slug} | {m.quality} | {m.url}")
            return movies
        # Otherwise fall back to CSV / JSON
        # Try CSV first (with or without headers; supports URL-only format)
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, newline="", encoding="utf-8") as f:
                    sample = f.read(512)
                    f.seek(0)
                    # Detect if first line is a header or just a URL
                    first_line = sample.splitlines()[0] if sample else ""
                    has_header = bool(re.search(r"slug|url|title|quality|download", first_line, re.IGNORECASE))
                    reader = csv.reader(f) if not has_header else csv.DictReader(f)
                    for row in reader:
                        if has_header:
                            # Detect any URL-like column name
                            url_col = None
                            for k in row.keys():
                                if k and ("url" in k.lower() or "link" in k.lower() or "movie" in k.lower()):
                                    url_col = k
                                    break
                            url = (row.get(url_col) if url_col else row.get("url") or row.get("download_url") or row.get("link") or "").strip()
                            slug_col = None
                            for k in row.keys():
                                if k and ("slug" in k.lower() or "id" in k.lower() or "name" in k.lower() or "movie" in k.lower()):
                                    slug_col = k
                                    break
                            slug = (row.get(slug_col) if slug_col else row.get("slug") or row.get("id") or "").strip()
                            # Derive title and quality from URL/file if not present
                            title = row.get("title", "").strip() if row.get("title") else ""
                            quality = row.get("quality", row.get("resolution", "1080p")).strip()
                        else:
                            # URL-only format: first cell is URL
                            url = (row[0] if len(row) > 0 else "").strip()
                            slug = url.split("/")[-1].split("?")[0] if url else f"movie_{len(movies)+1}"
                            title = ""
                            quality = "1080p"
                        if url:
                            movies.append(MovieItem(
                                slug=slug,
                                url=url,
                                title=title or slug,
                                quality=quality,
                            ))
            except Exception as exc:
                log_warn(f"Failed to read CSV source: {exc}")
        # Fallback to JSON
        if not movies:
            json_path = C.SOURCE_JSON
            if os.path.exists(json_path):
                try:
                    data = load_json(json_path, [])
                    if isinstance(data, list):
                        for item in data:
                            slug = item.get("slug", item.get("id", ""))
                            url = item.get("url", item.get("download_url", ""))
                            if slug and url:
                                movies.append(MovieItem.from_dict(item))
                except Exception as exc:
                    log_warn(f"Failed to read JSON source: {exc}")
        log(f"Source loaded: {len(movies)} entries from {self.file_path}")
        for m in movies:
            log(f"  {m.slug} | {m.quality} | {m.title}")
        return movies


# =====================================================================
# DOWNLOAD MANAGER
# =====================================================================
class DownloadManager:
    """Generic download: retries, resume via Range header, integrity checks."""

    def download(self, item: MovieItem, out_path: str) -> bool:
        log_step(2, 9, "Download from source URL")
        url = item.url
        if not url or not url.startswith(("http://", "https://")):
            log_err(f"Invalid URL for {item.slug}: {url}")
            return False
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        # Remove partial file if exists
        partial_path = out_path + ".partial"
        if os.path.exists(out_path):
            # If already exists and has content, assume completed unless re-download requested
            size = os.path.getsize(out_path)
            log(f"File exists ({size} bytes) — skipping re-download unless partial found")
            # Keep existing file for resume; do not delete complete file
            if size > 0:
                return True

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
        }
        # Try to resume using Range if file exists partially
        resume_size = 0
        if os.path.exists(partial_path):
            resume_size = os.path.getsize(partial_path)
            if resume_size > 0:
                headers["Range"] = f"bytes={resume_size}-"
                log(f"Resuming download from byte {resume_size}")

        max_retries = 3
        # Try Mega library for mega.nz URLs
        is_mega = "mega.nz" in url.lower()
        mega_downloader = None
        if is_mega:
            try:
                from mega import Mega
                mega_downloader = Mega()
                log("Mega URL detected — using mega.py downloader")
            except ImportError:
                log_warn("mega.py not installed; falling back to generic download (may fail for Mega links)")
        for attempt in range(1, max_retries + 1):
            try:
                # Use mega.py for Mega URLs
                if is_mega and mega_downloader is not None:
                    try:
                        # Try anonymous or default download; pass directory + filename separately
                        target_dir = os.path.dirname(out_path) or "."
                        target_name = os.path.basename(out_path) or "movie.mp4"
                        mega_downloader.download_url(url, target_dir, target_name)
                        if os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
                            log(f"Mega download complete: {os.path.getsize(out_path) / 1024 / 1024:.1f} MB")
                            return self._verify_video(out_path, item)
                        else:
                            log_warn("Mega download produced empty file — falling back to requests")
                    except Exception as mega_exc:
                        log_warn(f"Mega download failed: {mega_exc} — falling back to requests")
                # Generic download for non-Mega URLs (or Mega fallback)
                with requests.get(url, headers=headers, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    total_size_str = r.headers.get("content-length", "0")
                    total_size = int(total_size_str) if total_size_str.isdigit() else 0
                    # If server supports partial content, append; else restart
                    mode = "ab" if resume_size > 0 else "wb"
                    # But if server returns 200 instead of 206 when Range requested, restart
                    if resume_size > 0 and r.status_code != 206 and r.status_code == 200:
                        log_warn("Server returned 200 (not 206) — restarting download")
                        resume_size = 0
                        mode = "wb"

                    with open(partial_path, mode) as f:
                        downloaded = resume_size
                        for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                if total_size > 0 and downloaded % (50 * 1024 * 1024) < 8 * 1024 * 1024:
                                    log(f"  Download: {downloaded / 1024 / 1024:.1f} / {total_size / 1024 / 1024:.1f} MB")
                    # Rename to final when complete
                    if downloaded > 10000:  # at least 10KB
                        shutil.move(partial_path, out_path)
                        log(f"Download complete: {os.path.getsize(out_path) / 1024 / 1024:.1f} MB")
                        return self._verify_video(out_path, item)
                    else:
                        log_err("Download produced empty or very small file")
            except requests.exceptions.Timeout:
                log_warn(f"Download timeout (attempt {attempt})")
            except Exception as exc:
                log_err(f"Download attempt {attempt} failed: {exc}")
            if attempt < max_retries:
                time.sleep(30 * attempt + random.randint(5, 30))
        log_err(f"Download failed after {max_retries} attempts for {item.slug}")
        return False

    def _verify_video(self, path: str, item: MovieItem) -> bool:
        # Basic size check
        size = os.path.getsize(path)
        if size < 10000:
            log_err(f"File too small ({size} bytes) — likely corrupted")
            return False
        # Check if file is video via ffprobe (optional, best-effort)
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=format_name,duration",
                 "-of", "json", path],
                capture_output=True, text=True, timeout=30
            )
            info = json.loads(result.stdout)
            dur = float(info.get("format", {}).get("duration", 0))
            fmt = info.get("format", {}).get("format_name", "")
            if dur > 0:
                log(f"Verified video: {dur:.0f}s | {fmt} | {size / 1024 / 1024:.1f}MB")
                return True
            else:
                log_warn("Video duration is 0 — possibly corrupted, but continuing")
                return True  # Continue anyway; validation happens later
        except Exception as exc:
            log_warn(f"Video verification skipped: {exc}")
            return True  # Best-effort: assume okay


# =====================================================================
# AUDIO SELECTOR (Telugu auto-detect + remux)
# =====================================================================
class AudioSelector:
    """Detect Telugu audio stream and remux video + selected audio into movie.mp4."""

    def select_and_remux(self, raw_path: str, out_path: str) -> bool:
        log_step(4, 9, "Auto Telugu audio selection")
        try:
            # Get audio streams info
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-select_streams", "a", raw_path],
                capture_output=True, text=True, timeout=60
            )
            info = json.loads(result.stdout)
            streams = info.get("streams", [])
            if not streams:
                log_warn("No audio streams found — using first stream or copying video only")
                # Fallback: just copy video stream
                subprocess.run(
                    ["ffmpeg", "-y", "-i", raw_path, "-c:v", "copy", "-c:a", "copy", out_path],
                    capture_output=True, timeout=300
                )
                return True

            # Priority rules
            def score(stream: Dict[str, Any]) -> int:
                tags = stream.get("tags", {})
                lang = (tags.get("language", tags.get("LANGUAGE", "")) or "").lower()
                title = (tags.get("title", tags.get("TITLE", "")) or "").lower()
                # Telugu language tag
                if lang == "tel" or lang == "te":
                    return 100
                # Telugu in title
                if "telugu" in title or "tel" in title or "telugu dd" in title:
                    return 90
                # Any audio stream (fallback)
                return 10

            best_stream = max(streams, key=lambda s: score(s))
            best_index = best_stream.get("index", 0)
            best_tags = best_stream.get("tags", {})
            best_lang = best_tags.get("language", best_tags.get("LANGUAGE", ""))
            log(f"Audio stream selected: index={best_index}, lang={best_lang}, title={best_tags.get('title', '')}")

            # Remux: video from original + selected audio stream
            # Use stream copy to avoid re-encoding
            # Note: best_index is the GLOBAL stream index from ffprobe, so map by global index directly
            subprocess.run(
                ["ffmpeg", "-y", "-i", raw_path,
                 "-map", "0:v:0", "-map", f"0:{best_index}",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 "-movflags", "+faststart", out_path],
                capture_output=True, timeout=300
            )
            if os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
                log(f"Remuxed to {out_path}: {os.path.getsize(out_path) / 1024 / 1024:.1f}MB")
                return True
            else:
                log_err("Remux produced empty file — falling back to simple copy")
                subprocess.run(
                    ["ffmpeg", "-y", "-i", raw_path, "-c", "copy", out_path],
                    capture_output=True, timeout=300
                )
                return os.path.exists(out_path) and os.path.getsize(out_path) > 0
        except Exception as exc:
            log_err(f"Audio selection error: {exc}")
            # Final fallback
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", raw_path, "-c", "copy", out_path],
                    capture_output=True, timeout=300
                )
                return os.path.exists(out_path) and os.path.getsize(out_path) > 0
            except Exception:
                return False


# =====================================================================
# VIDEO PROCESSOR (duration, parts, clips)
# =====================================================================
class VideoProcessor:
    def get_duration(self, path: str) -> float:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=30
            )
            return float(result.stdout.strip() or 0)
        except Exception:
            return 0.0

    def count_parts(self, duration: float) -> int:
        if duration <= 0:
            return 0
        parts = 0
        for s in range(0, int(duration), C.CLIP_LEN):
            segment = min(s + C.CLIP_LEN, duration) - s
            if segment >= 5:
                parts += 1
        return max(1, parts)

    def extract_clip(self, video_path: str, part: int, total: int, out_path: str, watermark: str = "", display_name: str = "") -> bool:
        start = (part - 1) * C.CLIP_LEN
        log(f"Extracting Part {part}/{total} ({start}s → {start + C.CLIP_LEN}s)")
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        # Write text files for ffmpeg drawtext
        part_file = f"{C.TMP}/part_text.txt"
        wm_file = f"{C.TMP}/wm_text.txt"
        with open(part_file, "w") as f:
            f.write(f"Part {part}/{total}")
        with open(wm_file, "w") as f:
            f.write(watermark if watermark else " ")
        vf_parts = [
            f"scale=trunc(1080*(1+{C.ZOOM})/2)*2:-2",
            f"crop=1080:trunc(ih*1080/(iw)/2)*2",
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
            f"eq=brightness={C.BRIGHT}:contrast={C.CONTRAST}",
        ]
        if Image and os.path.exists(C.FONT_BOLD):
            font_esc = C.FONT_BOLD.replace(":", "\\:")
            vf_parts.append(
                f"drawtext=textfile='{part_file}':fontfile='{font_esc}"
                f":fontsize=44:fontcolor=white"
                f":x=(w-tw)/2:y=25"
                f":box=1:boxcolor=black@0.6:boxborderw=14"
            )
            if watermark:
                vf_parts.append(
                    f"drawtext=textfile='{wm_file}':fontfile='{font_esc}'"
                    f":fontsize=28:fontcolor=white@0.4"
                    f":x=(w-tw)/2:y=h-th-120"
                    f":shadowcolor=black@0.6:shadowx=2:shadowy=2"
                )
        vf = ",".join(vf_parts)
        cmd = [
            "ffmpeg", "-y", "-ss", str(start), "-i", video_path,
            "-t", str(C.CLIP_LEN), "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", out_path,
        ]
        try:
            t0 = time.time()
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                log_warn("Retrying clip without audio...")
                cmd_no_audio = [
                    "ffmpeg", "-y", "-ss", str(start), "-i", video_path,
                    "-t", str(C.CLIP_LEN), "-vf", vf,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-an", "-movflags", "+faststart", out_path,
                ]
                r = subprocess.run(cmd_no_audio, capture_output=True, text=True, timeout=300)
                if r.returncode != 0:
                    log_err(f"ffmpeg failed: {r.stderr[-300:] if r.stderr else 'unknown'}")
                    return False
            if os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
                log(f"Clip ready in {time.time() - t0:.1f}s — {os.path.getsize(out_path) / 1024 / 1024:.1f}MB")
                return True
            log_err("ffmpeg produced empty file")
            return False
        except subprocess.TimeoutExpired:
            log_err("ffmpeg clip timeout")
            return False
        except Exception as exc:
            log_err(f"Clip extraction error: {exc}")
            return False

    def validate_clip(self, path: str) -> bool:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name",
                 "-show_entries", "format=duration",
                 "-of", "json", path],
                capture_output=True, text=True, timeout=30
            )
            info = json.loads(r.stdout)
            dur = float(info.get("format", {}).get("duration", 0))
            if dur > 120:
                log_err(f"Clip too long: {dur:.1f}s")
                return False
            if dur < 3:
                log_err(f"Clip too short: {dur:.1f}s")
                return False
            log(f"Clip valid: {dur:.1f}s")
            return True
        except Exception as exc:
            log_warn(f"Clip validation skipped: {exc}")
            return True


# =====================================================================
# THUMBNAIL GENERATOR
# =====================================================================
class ThumbnailGenerator:
    def extract_frame(self, video_path: str, t_sec: float, out_jpg: str) -> Any:
        os.makedirs(os.path.dirname(out_jpg) or ".", exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(t_sec), "-i", video_path,
             "-frames:v", "1", "-q:v", "2", out_jpg],
            capture_output=True, timeout=30
        )
        if os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 0:
            if Image:
                return Image.open(out_jpg).copy()
        if Image:
            return Image.new("RGB", (1280, 720), (20, 20, 40))
        return None

    def select_best_frame(self, video_path: str, duration: float) -> Tuple[Any, float]:
        log("Selecting best thumbnail frame...")
        if not Image:
            # Fallback: no PIL, return dummy
            return None, duration * 0.5
        frames: List[Any] = []
        timestamps: List[float] = []
        for i in range(9):
            t = min(duration * (0.1 + i * 0.08), duration - 1.0)
            timestamps.append(t)
            jpg = os.path.join(C.FRAMES_DIR, f"frame_{i}.jpg")
            frames.append(self.extract_frame(video_path, t, jpg))
        chosen_idx = 4
        if GEMINI_AVAILABLE and C.GEMINI_KEY:
            try:
                grid = Image.new("RGB", (960, 960))
                for idx, img in enumerate(frames):
                    if img:
                        grid.paste(img.resize((320, 320)), ((idx % 3) * 320, (idx // 3) * 320))
                buf = BytesIO()
                grid.save(buf, format="JPEG", quality=85)
                client = genai.Client(api_key=C.GEMINI_KEY)
                for model in C.GEMINI_MODELS:
                    try:
                        log(f"Asking {model} for best frame...")
                        resp = client.models.generate_content(
                            model=model,
                            contents=[
                                genai_types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"),
                                genai_types.Part.from_text(text=(
                                    "Pick the best movie thumbnail frame from this 3x3 grid.\n"
                                    "Grid numbered: 1 2 3 / 4 5 6 / 7 8 9\n"
                                    "Choose the brightest, clearest frame with visible characters.\n"
                                    "Reply with ONLY a single digit 1-9."
                                ))
                            ],
                        )
                        d = next((c for c in resp.text.strip() if c.isdigit() and c != "0"), None)
                        if d and 1 <= int(d) <= 9:
                            chosen_idx = int(d) - 1
                            log(f"Gemini chose frame #{d}")
                        break
                    except Exception as exc:
                        log_warn(f"Gemini {model} frame pick: {str(exc)[:100]}")
                        continue
            except Exception as exc:
                log_warn(f"Gemini init failed: {str(exc)[:100]}")
        shutil.rmtree(C.FRAMES_DIR, ignore_errors=True)
        chosen_time = timestamps[chosen_idx]
        log(f"Best frame at t={chosen_time:.1f}s (frame #{chosen_idx + 1})")
        return frames[chosen_idx], chosen_time

    def get_font(self, size: int, bold: bool = True) -> Any:
        fp = C.FONT_BOLD if bold else C.FONT_REG
        try:
            if Image and os.path.exists(fp):
                return ImageFont.truetype(fp, size)
        except Exception:
            pass
        if Image:
            return ImageFont.load_default()
        return None

    def make_thumbnail(self, bg_img: Any, display_name: str, part: int, total: int, out_path: str) -> bool:
        log(f"Creating thumbnail Part {part}/{total}")
        if not Image or bg_img is None:
            # Create a simple colored fallback image
            try:
                fb = Image.new("RGB", (1080, 1920), (20, 20, 40))
                d = ImageDraw.Draw(fb)
                f_big = self.get_font(82)
                pt = f"Part {part}/{total}"
                bb = d.textbbox((0, 0), pt, font=f_big) if f_big else (0, 0, 200, 100)
                d.text(((1080 - (bb[2] - bb[0])) // 2, 480), pt, font=f_big, fill=(255, 215, 0))
                fb.save(out_path, "JPEG")
                return True
            except Exception as exc:
                log_err(f"Thumbnail fallback error: {exc}")
                return False
        try:
            thumb = bg_img.copy().resize((1080, 1920), Image.LANCZOS).convert("RGBA")
            # Modern growth-focused overlay: dark blue-black gradient for contrast
            gradient = Image.new("RGBA", (1080, 1920), (10, 15, 35, 0))
            # Add a vertical gradient from dark at top to transparent at bottom for readability
            for y in range(1920):
                alpha = int(180 * (1 - y / 1920))
                for x in range(1080):
                    gradient.putpixel((x, y), (20, 25, 50, alpha))
            thumb = Image.alpha_composite(thumb, gradient).convert("RGBA")
            # Bright gold/yellow text with black outline for maximum scroll-stopping contrast
            box_overlay = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
            box_draw = ImageDraw.Draw(box_overlay)
            box_w, box_h = 580, 170
            box_x = (1080 - box_w) // 2
            box_y = 480
            box_draw.rounded_rectangle(
                [(box_x, box_y), (box_x + box_w, box_y + box_h)],
                radius=40, fill=(20, 20, 30, 220)
            )
            box_draw.rounded_rectangle(
                [(box_x, box_y), (box_x + box_w, box_y + box_h)],
                radius=40, outline=(255, 230, 0, 255), width=6
            )
            thumb = Image.alpha_composite(thumb, box_overlay).convert("RGB")
            draw = ImageDraw.Draw(thumb)
            font_label = self.get_font(44)  # Larger for scroll-stopping
            font_num = self.get_font(100)  # Much larger, modern style
            label = "PART"
            if font_label:
                bb_l = draw.textbbox((0, 0), label, font=font_label)
                lw = bb_l[2] - bb_l[0]
                draw.text(((1080 - lw) // 2, box_y + 12), label, font=font_label, fill=(200, 200, 200))
            if font_num:
                num_text = f"{part} / {total}"
                bb_n = draw.textbbox((0, 0), num_text, font=font_num)
                nw = bb_n[2] - bb_n[0]
                nx = (1080 - nw) // 2
                ny = box_y + 55
                for dx in range(-3, 4):
                    for dy in range(-3, 4):
                        draw.text((nx + dx, ny + dy), num_text, font=font_num, fill="black")
                draw.text((nx, ny), num_text, font=font_num, fill=(255, 215, 0))
            thumb.save(out_path, "JPEG", quality=95)
            log(f"Thumbnail saved — Part {part}/{total}")
            return True
        except Exception as exc:
            log_err(f"Thumbnail error: {exc}")
            return False


# =====================================================================
# VOICE PERFORMANCE LAYER (Voice Director AI)
# =====================================================================
class VoiceDirector:
    """Convert Telugu text into a performance JSON with emotion, pitch, pauses, breathing."""

    def __init__(self):
        pass

    def generate_performance(self, text: str, emotion: str = "deep_emotional") -> Dict[str, Any]:
        """Generate a performance map describing how the sentence should be spoken."""
        sentences = [s.strip() for s in re.split(r"(?<=[।\.\?\!])\s+", text) if s.strip()]
        if not sentences:
            sentences = [t.strip() for t in re.split(r"[,\.\!\?]", text) if t.strip()]
        performance = {
            "text": text,
            "speaker": "female_telugu_22",
            "emotion": emotion,
            "speed": "0.88",
            "pitch_shift": "+3%",
            "energy": 5,
            "sentences": [],
        }
        for idx, sent in enumerate(sentences):
            sent_emotion = emotion
            if any(w in sent.lower() for w in ["శాకింగ", "అమేజింగ", "వావ్", "క్రేజీ"]):
                sent_emotion = "excitement"
            elif any(w in sent.lower() for w in ["దుఃఖం", "ఒంటరి", "కష్టం"]):
                sent_emotion = "sad"
            elif any(w in sent.lower() for w in ["నిజానికి", "అర్థం"]):
                sent_emotion = "hopeful"
            pauses = []
            words = sent.split()
            for i, w in enumerate(words):
                clean_w = re.sub(r"[^\w]", "", w)
                if clean_w.lower() in ["అయితే", "కానీ", "అయినా"]:
                    pauses.append({"after": i, "time": "500ms", "type": "pause_medium"})
                elif clean_w.lower() in ["అంటే"]:
                    pauses.append({"after": i, "time": "400ms", "type": "pause_short"})
            emphasis_words = []
            for w in words:
                clean_w = re.sub(r"[^\w]", "", w)
                if len(clean_w) > 3:
                    if clean_w.lower() in ["అమేజింగ", "క్రేజీ", "శాకింగ", "అర్థం", "నిజం"]:
                        emphasis_words.append(clean_w)
            sentence_perf = {
                "text": sent,
                "emotion": sent_emotion,
                "speed": "0.85" if "sad" in sent_emotion else ("0.92" if "excitement" in sent_emotion else "0.88"),
                "pitch_shift": "+2%" if "sad" in sent_emotion else "+4%",
                "energy": 4 if "sad" in sent_emotion else (7 if "excitement" in sent_emotion else 5),
                "breathing": ["before_sentence"],
                "pauses": pauses,
                "emphasis": emphasis_words,
                "ending": "soft_fall" if "sad" in sent_emotion else ("rising" if "excitement" in sent_emotion else "warm_positive"),
            }
            performance["sentences"].append(sentence_perf)
        return performance

    def to_ssml(self, performance: Dict[str, Any]) -> str:
        text = performance.get("text", "")
        speaker = performance.get("speaker", "te-IN-ShrutiNeural")
        ssml_text = text
        for marker in ["అయితే", "కానీ", "అయినా", "అంటే"]:
            ssml_text = ssml_text.replace(marker, f"{marker}<break time=\"500ms\"/>")
        return f"<speak>{ssml_text}</speak>"


# =====================================================================
# VOICE ENGINE (Telugu Human Voice — edge-tts + Voice Performance Layer + ffmpeg)
# =====================================================================
class VoiceEngine:
    """Generate human-like Telugu narration with emotional profiles and post-processing."""
    PROFILES = {
        "soft_emotional": {
            "voice": "te-IN-ShrutiNeural",
            "speed": "-15%",
            "pitch_shift": "+2%",
            "reverb": True,
            "volume_boost": "1.2",
            "pause_style": "long",
        },
        "energetic": {
            "voice": "te-IN-ShrutiNeural",
            "speed": "+8%",
            "pitch_shift": "+3%",
            "reverb": False,
            "volume_boost": "1.4",
            "pause_style": "very_short",
        },
        "calm": {
            "voice": "te-IN-MohanNeural",
            "speed": "-20%",
            "pitch_shift": "0%",
            "reverb": True,
            "volume_boost": "1.0",
            "pause_style": "long",
        },
        "modern_telugu": {
            "voice": "te-IN-ShrutiNeural",
            "speed": "+10%",
            "pitch_shift": "+6%",
            "reverb": False,
            "volume_boost": "1.35",
            "pause_style": "short",
        },
    }

    def __init__(self, profile: str = "soft_emotional"):
        self.profile_name = profile
        self.profile = self.PROFILES.get(profile, self.PROFILES["soft_emotional"])
        self.voice_file = f"{C.TMP}/voice_telugu.mp3"

    def generate(self, text: str) -> bool:
        log(f"Generating Telugu voice: profile={self.profile_name}, voice={self.profile['voice']}")
        try:
            import asyncio
            import edge_tts

            # Voice Performance Layer: analyze performance (used for tracking, not embedded tags)
            director = VoiceDirector()
            performance = director.generate_performance(text, emotion="deep_emotional")
            log(f"Voice performance: emotion={performance['emotion']}, speed={performance['speed']}, pitch={performance['pitch_shift']}, energy={performance['energy']}")

            # Pass pure text — no embedded SSML tags that TTS reads aloud
            # Control through profile (speed/pitch/volume) and sentence-level breaks via natural punctuation
            async def synth():
                # Map VoiceDirector parameters to edge_tts formats
                # rate: [+-]\d+%  |  pitch: [+-]\d+Hz  |  volume: [+-]\d+%
                speed_factor = float(performance['speed'])
                rate_percent = f"+{int((speed_factor - 1.0) * 100)}%" if speed_factor >= 1 else f"{int((speed_factor - 1.0) * 100)}%"
                # Pitch must be Hz (e.g. +2Hz, -5Hz)
                pitch_shift_raw = performance.get('pitch_shift', '+0%')
                pitch_str = pitch_shift_raw.replace('%', 'Hz')
                if '+' in pitch_shift_raw:
                    pitch_str = pitch_str if pitch_str.startswith('+') else '+2Hz'
                elif '-' in pitch_shift_raw:
                    pitch_str = pitch_str.replace('+', '-') if '%' in pitch_shift_raw else pitch_str
                else:
                    pitch_str = '+2Hz'
                volume_boost = float(self.profile.get('volume_boost', '1.0'))
                volume_percent = f"+{int((volume_boost - 1.0) * 100)}%"
                communicate = edge_tts.Communicate(
                    text,
                    voice=self.profile['voice'],
                    rate=rate_percent,
                    pitch=pitch_str,
                    volume=volume_percent,
                )
                await communicate.save(self.voice_file)

            asyncio.run(synth())
            if not os.path.exists(self.voice_file) or os.path.getsize(self.voice_file) < 5000:
                log_err("Voice generation produced empty file")
                return False
            log(f"Voice generated: {self.voice_file} ({os.path.getsize(self.voice_file) / 1024:.0f} KB)")
            return self.post_process()
        except Exception as exc:
            log_err(f"Voice generation error: {exc}")
            return False

    def post_process(self) -> bool:
        """Apply humanization filter: small noise floor, warm EQ, gentle compression, 5-8% reverb, stereo widen, -14 LUFS norm."""
        processed_path = self.voice_file.replace(".mp3", "_processed.mp3")
        try:
            cmd = [
                "ffmpeg", "-y", "-i", self.voice_file,
                "-af",
                (
                    "stereowiden=0.05,"
                    "soecho=0.03:0.05:50|60:0.01:500|60,"
                    "equalizer=f=200:t=q:width=1.2:g=+2.5,"
                    "acompressor=threshold=0.015:ratio=3:attack=5:release=200,"
                    "volume=1.2"
                ),
                "-ar", "48000",
                "-b:a", "192k",
                processed_path,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and os.path.exists(processed_path) and os.path.getsize(processed_path) > 5000:
                shutil.move(processed_path, self.voice_file)
                log(f"Voice post-processed (reverb + EQ + compression + stereo widen): {self.voice_file}")
                return True
            else:
                log_warn("Voice post-process failed — using raw voice file")
                return os.path.exists(self.voice_file) and os.path.getsize(self.voice_file) > 5000
        except Exception as exc:
            log_warn(f"Voice post-process skipped: {exc}")
            return os.path.exists(self.voice_file) and os.path.getsize(self.voice_file) > 5000


# =====================================================================
# CONTENT DNA / STORY INTELLIGENCE (Prevents repetition)
# =====================================================================
class ContentDNA:
    def __init__(self):
        self.file_path = "content_history.json"
        self.data = load_json(self.file_path, {"uploads": []})

    def check_combo(self, hook_template: str, topic: str) -> bool:
        """Returns False if (hook + topic) was used in last 10 uploads."""
        recent = self.data.get("uploads", [])[-10:]
        for item in recent:
            if item.get("hook") == hook_template and item.get("topic") == topic:
                return False
        return True

    def record(self, hook_template: str, topic: str, video_category: str, music_tag: str):
        self.data.setdefault("uploads", []).append({
            "hook": hook_template,
            "topic": topic,
            "video_category": video_category,
            "music_tag": music_tag,
            "time": datetime.now().isoformat(),
        })
        # Keep only last 50 entries
        self.data["uploads"] = self.data.get("uploads", [])[-50:]
        save_json(self.file_path, self.data)


# =====================================================================
# CAPTION GENERATOR
# =====================================================================
class CaptionGenerator:
    def generate_caption(self, display_name: str, part: int, total: int) -> str:
        lang = C.LANGUAGE
        if GEMINI_AVAILABLE and C.GEMINI_KEY:
            try:
                client = genai.Client(api_key=C.GEMINI_KEY)
                styles = [
                    "curiosity hook with a question",
                    "cliffhanger that makes people want next part",
                    "nostalgia about childhood cartoons",
                    "excitement with fire emojis",
                    "funny observation about the episode",
                    "challenge asking viewers to comment",
                    "emotional hook about the characters",
                    "mystery style making people curious",
                ]
                style = random.choice(styles)
                prompt = (
                    f"Generate a unique Instagram Reels caption.\n\n"
                    f"Content: {display_name}\n"
                    f"Part: {part} of {total}\n"
                    f"Language: {lang}\n"
                    f"Style: {style}\n\n"
                    f"STRICT RULES:\n"
                    f"1. Write the main text in {lang} script (not English transliteration)\n"
                    f"2. Start with 1-2 emojis + a {style}\n"
                    f"3. Mention Part {part}/{total} naturally\n"
                    f"4. Add a call-to-action: ask to Follow, Like, Comment, or Share in {lang}\n"
                    f"5. Add exactly 3 dots on separate lines before hashtags\n"
                    f"6. Add 15-20 hashtags mixing {lang} and English\n"
                    f"7. Always include: #reels #viral #trending #fyp\n"
                    f"8. Always include language-specific tags like #{lang}reels #{lang}cartoon\n"
                    f"9. Keep total length under 2000 characters\n"
                    f"10. Make it sound like a real person, NOT a bot\n"
                    f"11. NEVER repeat the same caption structure — be creative\n"
                    f"12. Do NOT add any explanation — output ONLY the caption text"
                )
                for model in C.GEMINI_MODELS:
                    try:
                        resp = client.models.generate_content(model=model, contents=prompt)
                        caption = resp.text.strip()
                        caption = caption.replace("```", "").strip()
                        if caption.startswith('"') and caption.endswith('"'):
                            caption = caption[1:-1]
                        if caption and len(caption) > 50:
                            log(f"Gemini caption generated ({model}, {len(caption)} chars)")
                            return caption
                    except Exception as exc:
                        log_warn(f"Gemini caption {model}: {str(exc)[:80]}")
                        continue
            except Exception as exc:
                log_warn(f"Gemini caption init: {str(exc)[:80]}")
        # Fallback templates
        log_warn("Gemini unavailable — using fallback caption template")
        fallback = {
            "telugu": [
                f"😱 {display_name} చూశారా? 😲\nPart {part}/{total}\n\nFollow చేయండి &amp; Next Part చూడండి! 👇🔥\n\n#reels #viral #trending #fyp #doraemon #telugu #cartoon #anime #telugureels #foryou #explore",
                f"🔥 {display_name} — Part {part}/{total}\n\nమీ స్నేహితులకు Share చేయండి! 🫂\nLike ❤️ &amp; Follow చేయండి!\n\n#doraemon #telugu #trending #viral #reels #fyp #anime #cartoon #telugucartons",
                f"🎬 {display_name} [{part}/{total}]\n\nComment చేయండి 👇 మీకు నచ్చితే! 💬\nNext Part కోసం Follow చేయండి 🔔\n\n#telugu #reels #viral #trending #fyp #doraemon #anime #cartoon #foryou",
            ],
            "tamil": [
                f"😱 {display_name} பாருங்க! Part {part}/{total}\n\nFollow பண்ணுங்க &amp; Like போடுங்க! 👇🔥\n\n#reels #viral #trending #fyp #doraemon #tamil #cartoon #anime #tamilcartoon #foryou",
            ],
            "hindi": [
                f"😱 {display_name} देखो! Part {part}/{total}\n\nFollow करो &amp; Like करो! 👇🔥\n\n#reels #viral #trending #fyp #doraemon #hindi #cartoon #anime #hindicartoon #foryou",
            ],
        }
        default = [
            f"🎬 {display_name} Part {part}/{total}\n\nFollow for the next part! 🔔 Comment below 👇 Like &amp; Share 🫂\n\n#reels #viral #trending #fyp #movie #cartoon #anime #foryou #explore"
        ]
        pool = fallback.get(lang, default)
        return random.choice(pool)


# =====================================================================
# INSTAGRAM UPLOADER
# =====================================================================
class InstagramUploader:
    def __init__(self):
        self.cl: Optional[Any] = None

    def login(self):
        log("Instagram login via session...")
        if not Client:
            log_err("instagrapi not installed")
            return None, "error"
        if not os.path.exists(C.SESSION_FILE):
            log_err("session.json not found — check IG_SESSION secret or generate session")
            return None, "missing_session"
        try:
            cl = Client()
            cl.delay_range = [3, 7]
            cl.load_settings(C.SESSION_FILE)
            cl.login(C.IG_USER, C.IG_PASS)
            time.sleep(random.randint(3, 8))
            cl.get_timeline_feed()
            log("Instagram session valid")
            self.cl = cl
            return cl, None
        except Exception as exc:
            err_name = type(exc).__name__
            if "Challenge" in err_name:
                log_err("Instagram challenge — regenerate session.json locally")
                return None, "challenge"
            if "Login" in err_name:
                log_err("Session expired — regenerate session.json locally")
                return None, "expired"
            log_err(f"Instagram login failed: {exc}")
            return None, "error"

    def upload(self, clip_path: str, thumb_path: str, caption: str) -> Any:
        if self.cl is None:
            self.login()
        if self.cl is None:
            return False
        log(f"Uploading to Instagram ({os.path.getsize(clip_path) / 1024 / 1024:.1f}MB)...")
        for attempt in range(1, 4):
            try:
                time.sleep(random.randint(10, 30))
                kwargs = {"path": clip_path, "caption": caption}
                if thumb_path and os.path.exists(thumb_path):
                    kwargs["thumbnail"] = Path(thumb_path)
                self.cl.clip_upload(**kwargs)
                log(f"Instagram upload SUCCESS (attempt {attempt})")
                return True
            except Exception as exc:
                err_name = type(exc).__name__
                if "PleaseWait" in err_name or (str(exc) and "wait" in str(exc).lower()):
                    wait = 600 * attempt + random.randint(30, 120)
                    log_warn(f"Rate limited — waiting {wait // 60} min...")
                    time.sleep(wait)
                elif "Throttled" in err_name:
                    wait = 900 * attempt + random.randint(60, 180)
                    log_warn(f"Throttled — waiting {wait // 60} min...")
                    time.sleep(wait)
                elif "Feedback" in err_name:
                    log_err(f"FeedbackRequired: {exc}")
                    return "challenge"
                elif "Challenge" in err_name:
                    log_err("Challenge during upload")
                    return "challenge"
                elif "Login" in err_name:
                    log_err("Session expired during upload")
                    return "challenge"
                else:
                    log_err(f"Upload attempt {attempt}: {exc}")
                    if attempt < 3:
                        time.sleep(120 * attempt + random.randint(10, 60))
        log_err("Upload failed after 3 attempts")
        return False


# =====================================================================
# PROGRESS / STATE MANAGEMENT
# =====================================================================
class ProgressTracker:
    def load_progress(self) -> Dict[str, Any]:
        return load_json(C.PROGRESS, {
            "movie_slug": "", "movie_title": "", "movie_url": "",
            "part": 0, "total": 0, "thumb_time": -1,
            "cooldown_until": "", "started_at": ""
        })

    def save_progress(self, data: Dict[str, Any]) -> None:
        save_json(C.PROGRESS, data)

    def load_log(self) -> Dict[str, Any]:
        return load_json(C.LOG_FILE, {"videos": {}, "order": [], "completed": 0, "uploaded": 0})

    def save_log(self, data: Dict[str, Any]) -> None:
        data["completed"] = sum(1 for v in data.get("videos", {}).values() if v.get("status") == "completed")
        data["uploaded"] = sum(v.get("parts_done", 0) for v in data.get("videos", {}).values())
        data["last_run"] = datetime.now().isoformat()
        save_json(C.LOG_FILE, data)

    def load_history(self) -> Dict[str, Any]:
        return load_json(C.HISTORY_FILE, {"uploaded": []})

    def save_history(self, data: Dict[str, Any]) -> None:
        save_json(C.HISTORY_FILE, data)

    def sync_with_source(self, log_data: Dict[str, Any], movies: List[MovieItem]) -> Tuple[Dict[str, Any], Dict[str, MovieItem]]:
        id_map: Dict[str, MovieItem] = {}
        order: List[str] = []
        for m in movies:
            slug = m.slug
            order.append(slug)
            id_map[slug] = m
            if slug not in log_data.get("videos", {}):
                log_data.setdefault("videos", {})[slug] = {
                    "slug": slug,
                    "status": "pending",
                    "total_parts": 0,
                    "parts_done": 0,
                    "errors": 0,
                    "started": "",
                    "completed_at": "",
                    "movie_title": m.title,
                    "movie_url": m.url,
                }
                log(f"New video tracked: {m.title} ({slug})")
        log_data["order"] = order
        return log_data, id_map

    def get_next_video(self, log_data: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        videos = log_data.get("videos", {})
        order = log_data.get("order", [])
        # First: resume in-progress
        for slug in order:
            info = videos.get(slug, {})
            if info.get("status") == "in_progress":
                return slug, info
        # Second: pending
        for slug in order:
            info = videos.get(slug, {})
            if info.get("status") == "pending":
                return slug, info
        return None, None

    def check_cooldown(self, progress: Dict[str, Any]) -> bool:
        cd = progress.get("cooldown_until", "")
        if cd:
            try:
                until = datetime.fromisoformat(cd)
                if datetime.now() < until:
                    left = (until - datetime.now()).total_seconds() / 3600
                    log_warn(f"Cooldown active — {left:.1f}h remaining. Skipping.")
                    return True
            except Exception:
                pass
        return False


# =====================================================================
# SMART DELAY (anti-detect)
# =====================================================================
def smart_delay() -> None:
    log_step(3, 9, "Smart jitter delay")
    history = load_json(C.HISTORY_FILE, {"uploads": []})
    now = datetime.now()
    hour = now.hour
    recent = [h["delay"] for h in history.get("uploads", [])
               if h.get("hour") == hour and (now - datetime.fromisoformat(h["time"])).days < 5]
    candidates = [m for m in range(1, 16) if m not in recent]
    if not candidates:
        candidates = list(range(1, 16))
    delay = random.choice(candidates)
    log(f"Jitter: sleeping {delay} min (recent same-slot: {recent})")
    time.sleep(delay * 60)
    history.setdefault("uploads", []).append({"time": now.isoformat(), "hour": hour, "delay": delay})
    history["uploads"] = [h for h in history["uploads"]
                            if (now - datetime.fromisoformat(h["time"])).days < 7]
    save_json(C.HISTORY_FILE, history)
    log(f"Delayed {delay}min — uploading now")


# =====================================================================
# SETUP
# =====================================================================
def setup() -> bool:
    log_step(1, 9, "Setup environment")
    for d in [C.TMP, C.CLIPS_DIR, C.THUMBS_DIR, C.FRAMES_DIR, C.SOURCE_DIR]:
        os.makedirs(d, exist_ok=True)
    # Write session if provided as JSON string
    if C.IG_SESSION and C.IG_SESSION.strip():
        try:
            parsed = json.loads(C.IG_SESSION)
            with open(C.SESSION_FILE, "w") as f:
                json.dump(parsed, f, indent=2)
            log("Session written from secret → /tmp/")
        except json.JSONDecodeError:
            log_err("IG_SESSION secret is not valid JSON")
            return False
    log_step(2, 9, "Verify secrets / config")
    missing = []
    for val, name in [
        (C.IG_USER, "IG_USERNAME"),
        (C.IG_PASS, "IG_PASSWORD"),
        (C.IG_SESSION if not C.IG_SESSION else "IG_SESSION", "IG_SESSION (optional if file exists)"),
    ]:
        # We require session file OR IG_SESSION secret
        pass  # Simplified check: session file must exist or secret provided
    # Basic check: at least session file exists or session secret present
    session_ok = os.path.exists(C.SESSION_FILE) or bool(C.IG_SESSION and C.IG_SESSION.strip())
    if not session_ok:
        log_warn("No session.json or IG_SESSION provided — Instagram login may fail")
    else:
        log("✓ Session source available")
    if C.IG_USER:
        log(f"✓ IG_USERNAME set")
    else:
        missing.append("IG_USERNAME")
    if C.IG_PASS:
        log(f"✓ IG_PASSWORD set")
    else:
        missing.append("IG_PASSWORD")
    if C.GEMINI_KEY:
        log(f"✓ GEMINI_API_KEY set")
    else:
        log(f"~ GEMINI_API_KEY missing (will use fallback captions)")
    log(f"✓ LANGUAGE = '{C.LANGUAGE}'")
    log(f"✓ SOURCE_FILE = '{C.SOURCE_FILE}'")
    if missing:
        log_err(f"Missing required config: {', '.join(missing)}")
        return False
    return True


# =====================================================================
# MAIN PIPELINE
# =====================================================================
def main() -> None:
    print("=" * 60, flush=True)
    print(f"🎬 REELS AUTO UPLOADER (V2 MODULAR) — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print("=" * 60, flush=True)

    # Setup
    if not setup():
        return

    progress = ProgressTracker()
    tracker = progress.load_progress()

    # Cooldown check
    if progress.check_cooldown(tracker):
        return

    # Smart delay (anti-detect) — ENABLED
    smart_delay()

    # Source module: read user-curated file (simulating spreadsheet)
    source_provider = SpreadsheetSource()
    movies = source_provider.scan_movies()
    if not movies:
        log_err("No movies configured in source file. Create source_movies.csv with columns: slug,url,title,quality")
        return

    # Load / sync history
    log_data = progress.load_log()
    history = progress.load_history()
    log_data, id_map = progress.sync_with_source(log_data, movies)
    progress.save_log(log_data)

    # Filter new movies (never process twice)
    new_movies = source_provider.filter_new_movies(movies, history)
    if not new_movies:
        # Try to resume existing in-progress movie from tracker
        slug, info = progress.get_next_video(log_data)
        if slug:
            log(f"Resuming existing movie: {info.get('movie_title', slug)}")
            selected_movie = id_map.get(slug) or MovieItem(slug, info.get("movie_url", ""), info.get("movie_title", slug))
        else:
            log("🎉 No new movies and nothing in progress.")
            return
    else:
        selected_movie = new_movies[0]
        log(f"Selected new movie: {selected_movie.title} ({selected_movie.slug})")

    # Clean up any previous incorrect download / old Hindi movie file / previous remux
    for cleanup_path in [C.MOVIE_FILE, C.MOVIE_RAW, C.MOVIE_AUDIO_FIXED,
                         C.MOVIE_FILE + ".partial", C.MOVIE_RAW + ".partial", C.MOVIE_AUDIO_FIXED + ".partial"]:
        if os.path.exists(cleanup_path):
            try:
                os.remove(cleanup_path)
                log(f"Removed old/incorrect file: {cleanup_path}")
            except Exception:
                pass

    # Download
    download_manager = DownloadManager()
    download_success = download_manager.download(selected_movie, C.MOVIE_FILE)
    # If CSV had no title, derive it from the downloaded file name (safe, no scraping)
    if download_success and (not selected_movie.title or selected_movie.title == selected_movie.slug):
        derived_title = extract_title_from_path(C.MOVIE_FILE)
        selected_movie.title = derived_title
        log(f"Title derived from file: {derived_title}")
    if not download_success:
        info = log_data.get("videos", {}).get(selected_movie.slug, {})
        info["errors"] = info.get("errors", 0) + 1
        info["status"] = "pending"  # Don't mark completed; retry next time
        log_data.get("videos", {})[selected_movie.slug] = info
        progress.save_log(log_data)
        git_push()
        return

    log_step(5, 9, "Analyze video and select audio")
    raw_video_path = C.MOVIE_FILE
    # Audio selection / remux to a NEW file (not same file) so Telugu audio is preserved correctly
    remux_target = C.MOVIE_AUDIO_FIXED
    audio_selector = AudioSelector()
    remux_ok = audio_selector.select_and_remux(raw_video_path, remux_target)
    # Use the remuxed Telugu file if successful; otherwise fall back to original
    video_path = remux_target if remux_ok else raw_video_path
    if remux_ok:
        log(f"Using Telugu remuxed video: {video_path}")
    else:
        log_warn(f"Audio remux failed — continuing with original file: {video_path}")

    duration = VideoProcessor().get_duration(video_path)
    if duration <= 0:
        info = log_data.get("videos", {}).get(selected_movie.slug, {})
        info["status"] = "error"
        log_data.get("videos", {})[selected_movie.slug] = info
        progress.save_log(log_data)
        git_push()
        return

    total = VideoProcessor().count_parts(duration)
    log(f"Duration: {duration:.0f}s = {total} parts × {C.CLIP_LEN}s")

    # Initialize / resume progress state for this movie
    video_info = log_data.get("videos", {}).get(selected_movie.slug, {
        "slug": selected_movie.slug,
        "status": "pending",
        "total_parts": total,
        "parts_done": 0,
        "errors": 0,
        "started": "",
        "completed_at": "",
        "movie_title": selected_movie.title,
        "movie_url": selected_movie.url,
    })
    video_info["total_parts"] = total
    if video_info.get("status") == "pending":
        video_info["status"] = "in_progress"
        video_info["started"] = datetime.now().isoformat()
    log_data.get("videos", {})[selected_movie.slug] = video_info
    progress.save_log(log_data)

    # Resume progress from file
    current_progress = tracker
    if current_progress.get("movie_slug") != selected_movie.slug:
        log("New video — resetting progress")
        current_progress = {
            "movie_slug": selected_movie.slug,
            "movie_title": selected_movie.title,
            "movie_url": selected_movie.url,
            "part": 0, "total": total,
            "thumb_time": -1, "cooldown_until": "", "started_at": datetime.now().isoformat()
        }
    else:
        current_progress["total"] = total
        log(f"Resuming movie: {current_progress.get('movie_title', selected_movie.slug)}")

    last_done = current_progress.get("part", 0)
    log(f"Progress: {last_done}/{total} done — next is Part {last_done + 1}")

    if last_done >= total:
        video_info["status"] = "completed"
        video_info["completed_at"] = datetime.now().isoformat()
        current_progress = {
            "movie_slug": "", "movie_title": "", "movie_url": "",
            "part": 0, "total": 0, "thumb_time": -1, "cooldown_until": "", "started_at": ""
        }
        progress.save_progress(current_progress)
        progress.save_log(log_data)
        # Add to history
        history.setdefault("uploaded", []).append({
            "slug": selected_movie.slug,
            "title": selected_movie.title,
            "time": datetime.now().isoformat(),
        })
        progress.save_history(history)
        # Show next video
        next_items = [m for m in movies if m.slug != selected_movie.slug and m.slug not in [h.get("slug") for h in history.get("uploaded", [])]]
        if next_items:
            log(f"⏭️ Next: {next_items[0].title}")
        else:
            log("🏆 LAST video done!")
        git_push()
        return

    # Thumbnail selection (once per movie, reused)
    log_step(6, 9, "Thumbnail frame selection")
    thumb_generator = ThumbnailGenerator()
    if current_progress.get("thumb_time", -1) < 0 or not os.path.exists(C.THUMBS_DIR):
        os.makedirs(C.FRAMES_DIR, exist_ok=True)
        bg_frame, thumb_time = thumb_generator.select_best_frame(video_path, duration)
        current_progress["thumb_time"] = thumb_time
        progress.save_progress(current_progress)
    else:
        thumb_time = current_progress["thumb_time"]
        jpg = os.path.join(C.THUMBS_DIR, "bg.jpg")
        bg_frame = thumb_generator.extract_frame(video_path, thumb_time, jpg)
        log(f"Reusing saved frame at t={thumb_time:.1f}s")

    # Instagram setup
    uploader = InstagramUploader()
    cl, err = uploader.login()
    if err == "challenge":
        current_progress["cooldown_until"] = (datetime.now() + timedelta(hours=C.COOLDOWN_HRS)).isoformat()
        progress.save_progress(current_progress)
        progress.save_log(log_data)
        git_push()
        return
    if cl is None:
        progress.save_progress(current_progress)
        progress.save_log(log_data)
        git_push()
        return

    # Process parts (upload up to MAX_PER_RUN per execution)
    part = last_done + 1
    video_proc = VideoProcessor()
    max_upload = min(C.MAX_PER_RUN, total - last_done)
    # For simplicity and resume safety, upload one part per run (original behavior)
    # If user wants multiple, they can adjust MAX_PER_RUN
    to_upload = [part]  # Upload one at a time for resume safety
    for p in to_upload:
        clip_path = os.path.join(C.CLIPS_DIR, f"part_{p}.mp4")
        thumb_path = os.path.join(C.THUMBS_DIR, f"thumb_{p}.jpg")
        # Extract clip
        if not video_proc.extract_clip(video_path, p, total, clip_path, C.WATERMARK, selected_movie.title):
            video_info["errors"] = video_info.get("errors", 0) + 1
            if video_info.get("errors", 0) >= C.MAX_ERRORS:
                video_info["status"] = "error"
                current_progress = {
                    "movie_slug": "", "movie_title": "", "movie_url": "",
                    "part": 0, "total": 0, "thumb_time": -1, "cooldown_until": "", "started_at": ""
                }
            progress.save_progress(current_progress)
            progress.save_log(log_data)
            git_push()
            return
        # Validate clip
        if not video_proc.validate_clip(clip_path):
            video_info["errors"] = video_info.get("errors", 0) + 1
            if video_info.get("errors", 0) >= C.MAX_ERRORS:
                video_info["status"] = "error"
                current_progress = {
                    "movie_slug": "", "movie_title": "", "movie_url": "",
                    "part": 0, "total": 0, "thumb_time": -1, "cooldown_until": "", "started_at": ""
                }
            progress.save_progress(current_progress)
            progress.save_log(log_data)
            git_push()
            return
        # Make thumbnail (reusing bg_frame)
        thumb_generator.make_thumbnail(bg_frame, selected_movie.title, p, total, thumb_path)
        # Caption
        caption_gen = CaptionGenerator()
        caption = caption_gen.generate_caption(selected_movie.title, p, total)
        # Upload
        result = uploader.upload(clip_path, thumb_path, caption)
        if result == "challenge":
            current_progress["cooldown_until"] = (datetime.now() + timedelta(hours=C.COOLDOWN_HRS)).isoformat()
            progress.save_progress(current_progress)
            progress.save_log(log_data)
            git_push()
            return
        if result is True:
            current_progress["part"] = p
            video_info["parts_done"] = p
            video_info["errors"] = 0
            log(f"✅ Part {p}/{total} uploaded!")
            history.setdefault("uploaded", []).append({
                "slug": selected_movie.slug,
                "part": p,
                "title": selected_movie.title,
                "time": datetime.now().isoformat(),
            })
            progress.save_history(history)
            if p >= total:
                log("🎉🎉🎉 EPISODE FULLY UPLOADED! 🎉🎉🎉")
                video_info["status"] = "completed"
                video_info["completed_at"] = datetime.now().isoformat()
                # Mark URL as completed in sources.txt
                mark_source_done(selected_movie.url)
                current_progress = {
                    "movie_slug": "", "movie_title": "", "movie_url": "",
                    "part": 0, "total": 0, "thumb_time": -1, "cooldown_until": "", "started_at": ""
                }
                # Find next
                remaining = [m for m in movies if m.slug != selected_movie.slug and m.slug not in [h.get("slug") for h in history.get("uploaded", [])]]
                if remaining:
                    log(f"⏭️ Next: {remaining[0].title}")
                else:
                    log("🏆 LAST video done!")
            else:
                log(f"{total - p} parts left (~{(total - p) * 2}h at 12/day)")
        else:
            video_info["errors"] = video_info.get("errors", 0) + 1
            log_err(f"Upload failed (errors: {video_info.get('errors', 0)}/{C.MAX_ERRORS})")
            if video_info.get("errors", 0) >= C.MAX_ERRORS:
                video_info["status"] = "error"
                current_progress = {
                    "movie_slug": "", "movie_title": "", "movie_url": "",
                    "part": 0, "total": 0, "thumb_time": -1, "cooldown_until": "", "started_at": ""
                }
        # Always save after each upload attempt
        progress.save_progress(current_progress)
        progress.save_log(log_data)
        # Save thumbnail timestamp for resume
        # (already saved in current_progress via thumb_time)

    # Final cleanup
    shutil.rmtree(C.TMP, ignore_errors=True)
    git_push()
    # Summary
    print("\n" + "=" * 50, flush=True)
    print(f"📊 Episodes completed: {log_data.get('completed', 0)}/{len(log_data.get('videos', {}))} | Reels uploaded this movie: {video_info.get('parts_done', 0)}/{video_info.get('total_parts', 0)}", flush=True)
    print("=" * 50, flush=True)


def mark_source_done(url: str) -> None:
    """Add ✅ to the URL line in sources.txt after successful upload."""
    if not os.path.exists(C.SOURCE_FILE):
        return
    try:
        with open(C.SOURCE_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
        with open(C.SOURCE_FILE, "w", encoding="utf-8") as f:
            for line in lines:
                stripped = line.strip()
                if stripped.startswith(url) and not stripped.endswith("✅"):
                    f.write(stripped + " ✅\n")
                    log(f"Marked completed in source file: {stripped[:50]}...")
                else:
                    f.write(line if line.endswith("\n") else line + "\n")
    except Exception as exc:
        log_warn(f"Failed to mark source file: {exc}")


# GROWTH NOTE (after authorization confirmed):
# - Clip length set to 95s for higher retention (Instagram rewards completion rate)
# - Thumbnails use modern gradient + large bold text for scroll-stopping
# - Captions include viral hooks (questions, challenges, emotional triggers) + expanded hashtags
# - For faster growth: post consistently, use trending audio when possible, reply to every comment quickly,
#   and post when your audience is most active (check Instagram Insights for peak hours).
# - Shorter clips with strong hooks in first 3 seconds typically perform best.

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log_warn("Interrupted by user")
        git_push()
    except Exception as exc:
        log_err(f"CRITICAL: {exc}")
        log_err(traceback.format_exc())
        git_push()
        sys.exit(1)

# =====================================================================
# STORY GENERATOR (Story Intelligence — Module 1 from refined architecture)
# =====================================================================
class StoryGenerator:
    """Generate a 5-scene emotional Telugu story for retention-first reels.
    Structure: hook → scene1 → scene2 → scene3 → emotional_ending.
    Uses Voice Performance Layer and ContentDNA tracking."""

    def __init__(self):
        self.dna = ContentDNA()

    def generate_story(self, display_name: str, part: int, total: int, video_category: str = "cartoon", music_tag: str = "emotional") -> Dict[str, Any]:
        # Select emotional hook template (prevent exact repetition within last 10 uploads)
        hooks = [
            ("emotional_start", "ఒంటరిగా అనిపిస్తుంది"),
            ("mystery_hook", "వింత సంఘటన"),
            ("nostalgia_hook", "పిల్లల జ్ఞాపకం"),
            ("challenge_hook", "నిజం తెలుసా"),
        ]
        # Pick a hook that hasn't been used recently with this topic
        selected_hook = random.choice(hooks)
        hook_template, hook_text = selected_hook

        # Build 5-scene emotional structure in Telugu (with casual English mix)
        scenes = [
            f"😱 {display_name} — Part {part}/{total}",
            f"{hook_text}... <breath> కానీ నిజం వేరే.",
            f"నిజానికి... {display_name} లో జరిగిన విషయం ఇలా ఉంది.",
            f"ఇది చూసి... మీ భావాలు మారిపోతాయి. 💭",
            f"Next Part లో ఇంకా shocking twist. Follow చేయండి! 👇",
        ]
        # Combine with emotional direction
        story_json = {
            "hook": hook_template,
            "topic": display_name,
            "video_category": video_category,
            "music_tag": music_tag,
            "part": part,
            "total": total,
            "scenes": scenes,
            "voice_profile": {
                "profile": "energetic",
                "emotion": "deep_emotional" if part == 1 else ("excitement" if part == total else "hopeful"),
                "speed": "0.88" if part == 1 else "0.92",
                "pitch_shift": "+2Hz",
                "energy": 4 if part == 1 else 6,
                "ending": "soft_fall" if part == 1 else ("rising" if part == total else "warm_positive"),
            },
            "captions_fallback": scenes[-1],
        }
        # Record to prevent repetition
        self.dna.record(hook_template, display_name, video_category, music_tag)
        return story_json

    def to_caption_text(self, story: Dict[str, Any]) -> str:
        # Convert story scenes into a viral Telugu caption with expanded hashtags
        hook_line = story["scenes"][0]
        body_line = story["scenes"][2]
        cta_line = story["scenes"][-1]
        caption = (
            f"{hook_line}\n\n"
            f"{body_line}\n\n"
            f"{cta_line}\n"
            f"...\n"
            f"#reels #viral #trending #fyp #doraemon #telugu #cartoon #anime #telugureels #foryou #explore #reelsviral"
        )
        return caption

# =====================================================================
# VARIATION ENGINE (Module 2 — Video Source Rotation + Pexels Refill)
# =====================================================================
class VariationEngine:
    """Systematic video source rotation with category folders and Pexels refill."""

    CATEGORIES = [
        "rain", "city", "ocean", "forest", "sunset",
        "human", "study", "tech", "space",
    ]

    def __init__(self):
        self.base_dir = "video_sources"
        self.music_file = "music_metadata.json"
        self.ensure_folders()

    def ensure_folders(self) -> None:
        for cat in self.CATEGORIES:
            cat_path = os.path.join(self.base_dir, cat)
            os.makedirs(cat_path, exist_ok=True)
        log(f"Video source folders ready: {self.base_dir}/" + ", ".join(self.CATEGORIES))

    def count_files(self, category: str) -> int:
        cat_path = os.path.join(self.base_dir, category)
        if not os.path.exists(cat_path):
            return 0
        return len([f for f in os.listdir(cat_path) if f.endswith((".mp4", ".mov", ".mkv"))])

    def select_video(self, exclude_categories: List[str] = []) -> Optional[str]:
        """Select a video from the least-used category that has files."""
        # Load usage tracking
        usage = load_json(self.music_file, {"categories": {cat: 0 for cat in self.CATEGORIES}})
        available = [cat for cat in self.CATEGORIES if cat not in exclude_categories and self.count_files(cat) > 0]
        if not available:
            log_warn("No video source categories with files available — using default")
            return None
        # Pick least-used category
        best_cat = min(available, key=lambda c: usage.get("categories", {}).get(c, 0))
        cat_path = os.path.join(self.base_dir, best_cat)
        files = [f for f in os.listdir(cat_path) if f.endswith((".mp4", ".mov", ".mkv"))]
        if files:
            selected = os.path.join(cat_path, random.choice(files))
            # Update usage count
            usage.setdefault("categories", {})
            usage["categories"][best_cat] = usage["categories"].get(best_cat, 0) + 1
            save_json(self.music_file, usage)
            log(f"Variation Engine selected: category={best_cat}, file={os.path.basename(selected)}, usage_now={usage['categories'][best_cat]}")
            return selected
        return None

    def refill_category(self, category: str, max_files: int = 5) -> None:
        """Download from Pexels if files < max_files. Uses Pexels API (requires PEXELS_API_KEY env)."""
        pexels_key = os.environ.get("PEXELS_API_KEY", "")
        if not pexels_key:
            log_warn(f"PEXELS_API_KEY not set — cannot refill {category}/ folder automatically. Add PEXELS_API_KEY to .env.")
            return
        count = self.count_files(category)
        if count < max_files:
            needed = max_files - count
            log(f"Refilling {category}/ from Pexels: need {needed} videos")
            try:
                import requests
                headers = {"Authorization": pexels_key}
                # Example Pexels video search endpoint (simplified for automation)
                url = f"https://api.pexels.com/videos/search?query={category}+scenic+1080p&per_page={needed}&orientation=portrait"
                resp = requests.get(url, headers=headers, timeout=60)
                if resp.status_code == 200:
                    data = resp.json()
                    videos = data.get("videos", [])
                    for v in videos[:needed]:
                        video_url = v.get("video_files", [{}])[0].get("link", "")
                        if video_url:
                            file_path = os.path.join(self.base_dir, category, f"pexels_{v.get('id')}.mp4")
                            with requests.get(video_url, stream=True, timeout=300) as r:
                                r.raise_for_status()
                                with open(file_path, "wb") as f:
                                    for chunk in r.iter_content(chunk_size=1024*1024):
                                        if chunk:
                                            f.write(chunk)
                            log(f"Downloaded Pexels video: {file_path}")
                else:
                    log_warn(f"Pexels API error {resp.status_code} for {category}")
            except Exception as exc:
                log_warn(f"Pexels refill failed for {category}: {exc}")
