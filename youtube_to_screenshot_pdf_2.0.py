#!/usr/bin/env python3
"""
YouTube -> Screenshot PDF Studio  (PyQt6)
=========================================

Paste a YouTube link, capture de-duplicated screenshots, review them, and export
a PDF with (optionally) a cleaned-up, readable English transcript under each one.

Install:
    pip install PyQt6 yt-dlp opencv-python numpy reportlab youtube-transcript-api
    (youtube-transcript-api is optional - it is only a transcript fallback)

Run:
    python yt_screenshot_pdf.py

Notes:
  * ffmpeg is NOT required (only a video-only stream is downloaded).
  * Transcripts come from YouTube's own English captions (manual preferred,
    auto-generated as fallback). No timestamps are written to the PDF.
  * "Refine" cleans the transcript offline (removes [Music]/fillers/stutters,
    merges rolling-caption overlaps, adds punctuation + paragraphs).
  * Optional: "Polish with Claude" sends each transcript chunk to the Anthropic
    API for a higher-quality rewrite (needs your own API key; never saved).
  * Use only for videos you have the right to download/copy.
"""
from __future__ import annotations

import bisect
import importlib
import subprocess
import html
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional
from xml.sax.saxutils import escape as xml_escape

try:
    import cv2
    import numpy as np
    import yt_dlp  # noqa: F401
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, letter, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (Image as RLImage, KeepTogether, PageBreak,
                                    Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
except ImportError as exc:  # pragma: no cover
    sys.exit(f"Missing dependency: {exc.name}\n"
             "Install everything with:\n"
             "  pip install PyQt6 yt-dlp opencv-python numpy reportlab")

from PyQt6.QtCore import QSize, Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QIcon, QPixmap
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMainWindow, QMessageBox, QPlainTextEdit,
                             QProgressBar, QPushButton, QScrollArea, QSizePolicy,
                             QSpinBox, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

# --------------------------------------------------------------------------- #
#  Data models
# --------------------------------------------------------------------------- #


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class VideoInfo:
    source_url: str
    video_id: str = ""
    title: str = "Untitled video"
    channel: str = ""
    duration: float = 0.0
    thumbnail_bytes: Optional[bytes] = None
    transcript_kind: Optional[str] = None  # "manual" | "auto" | None
    transcript_lang: str = ""
    segments: List[Segment] = field(default_factory=list)
    is_local: bool = False
    ctx: Optional[tuple] = None  # (cookies, client) that worked for this video


@dataclass
class PlaylistInfo:
    source_url: str
    title: str = "Playlist"
    channel: str = ""
    entries: List[dict] = field(default_factory=list)
    is_local: bool = False


@dataclass(eq=False)  # identity comparison on purpose
class Shot:
    time: float
    jpeg: bytes
    w: int
    h: int


# --------------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------------- #


def parse_time(text: str) -> Optional[float]:
    """'', '90', '1:30', '01:02:03' -> seconds (None if blank)."""
    text = text.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"Invalid time: {text!r}")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"Invalid time: {text!r}") from None
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def fmt_duration(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "", name).strip(" .")
    return name[:80] or "youtube_screenshots"


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{root} ({n}){ext}"):
        n += 1
    return f"{root} ({n}){ext}"


# --------------------------------------------------------------------------- #
#  Transcript download + parsing
# --------------------------------------------------------------------------- #


def _pick_english(table: dict):
    """Return (url, lang) of the best English json3 track in a yt-dlp caption table."""
    langs = [l for l in table if l == "en" or l.startswith(("en-", "en_"))]
    langs.sort(key=lambda l: (l != "en", l != "en-US", l))
    for lang in langs:
        for fmt in table[lang]:
            if fmt.get("ext") == "json3" and fmt.get("url"):
                return fmt["url"], lang
    return None


def parse_json3(data: dict) -> List[Segment]:
    segs: List[Segment] = []
    for ev in data.get("events", []):
        if "segs" not in ev:
            continue
        text = "".join(s.get("utf8", "") for s in ev["segs"]).replace("\n", " ").strip()
        if not text:
            continue
        start = ev.get("tStartMs", 0) / 1000.0
        dur = ev.get("dDurationMs", 0) / 1000.0
        segs.append(Segment(start, start + dur, text))
    segs.sort(key=lambda s: s.start)
    return segs


# --------------------------------------------------------------------------- #
#  Robustness layer: URL cleanup, fallbacks, auto-update, local files
# --------------------------------------------------------------------------- #


def playlist_id(text: str) -> Optional[str]:
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", text)
    return m.group(1) if m else None


def normalize_url(text: str, keep_playlist: bool = False) -> str:
    text = text.strip().strip("\"'<> ")
    if text.lower().startswith("file://"):
        return urllib.request.url2pathname(urllib.parse.urlparse(text).path)
    if keep_playlist and "youtu" in text.lower():
        pid = playlist_id(text)
        if pid:
            return f"https://www.youtube.com/playlist?list={pid}"
    m = re.search(
        r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|embed/|live/|v/))"
        r"([A-Za-z0-9_-]{11})", text)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text) and not os.path.exists(text):
        return f"https://www.youtube.com/watch?v={text}"
    return text


_UNIVERSAL_FALLBACK = (
    "\n\nAlways-works fallback: get the video file any way you like (another downloader or a "
    "screen recording), then click 'Open local video...'. Add a .srt/.vtt file with "
    "'Load transcript file...' if you want transcripts.")

_HINTS = [
    (("permission denied", "permissionerror", "being used by another process"),
     "Windows could not write that file. If the PDF is open in a viewer, close it - or pick a different "
     "file name / folder under 'Save as'."),
    (("cookie database", "cookies database", "could not copy", "dpapi", "failed to decrypt"),
     "The app could not read your browser's cookies. Close that browser completely and retry, "
     "choose Firefox (most reliable on Windows), or export a cookies.txt file and select it under "
     "'Connection & reliability'."),
    (("sign in to confirm", "not a bot", "confirm you\u2019re not a bot", "confirm you're not a bot"),
     "YouTube wants a logged-in session. Log in to YouTube in Firefox, then set 'Browser cookies' to "
     "Firefox (or Auto). Changing network / turning off a VPN often helps too."),
    (("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
     "Age-restricted video: log in with an age-verified account in your browser and use 'Browser cookies'."),
    (("private video", "members-only", "members only", "join this channel", "requires payment"),
     "Private / members-only / paid video - it can only be fetched with cookies from an account that has access."),
    (("your country",),
     "The video is region-blocked. Use a VPN for that region or open a local copy of the video."),
    (("live stream", "live event", "premieres in", "is live"),
     "Live streams and upcoming premieres can't be captured. Try again after the stream has ended."),
    (("429", "too many requests"),
     "YouTube is rate-limiting you. Wait 10-30 minutes, change network, or use 'Browser cookies'."),
    (("403", "forbidden"),
     "YouTube rejected the request. Click 'Update yt-dlp now', then retry with 'Browser cookies' = Auto."),
    (("js runtime", "javascript runtime", "n challenge", "nsig", "signature extraction", "player response"),
     "YouTube changed its player. Click 'Update yt-dlp now'. If it persists, install Deno (https://deno.com) "
     "or Node.js and restart the app."),
    (("requested format is not available",),
     "That quality isn't offered for this video. Pick another quality (e.g. 480p) or update yt-dlp."),
    (("opencv could not open", "codec"),
     "The video codec can't be read here. Try 480p or 720p."),
    (("video unavailable", "been removed", "no longer available", "account associated", "terminated"),
     "YouTube says this video is unavailable. Double-check the link in your browser."),
    (("timed out", "timeout", "getaddrinfo", "urlopen error", "connection reset", "connection refused",
      "network is unreachable", "ssl"),
     "Network problem. Check your internet, VPN/proxy, firewall or antivirus, then retry."),
]


def explain_error(msg: str) -> str:
    low = msg.lower()
    for keys, hint in _HINTS:
        if any(k in low for k in keys):
            return hint + _UNIVERSAL_FALLBACK
    return ("Click 'Update yt-dlp now', set 'Browser cookies' to Auto, and try again." + _UNIVERSAL_FALLBACK)


_FATAL = ("private video", "members-only", "members only", "join this channel", "been removed",
          "account associated with this video has been terminated", "live stream", "is live")


def is_fatal(msg: str) -> bool:
    low = msg.lower()
    return any(k in low for k in _FATAL)


CLIENT_PLANS = [None, ["tv"], ["web_safari"], ["mweb"], ["android_vr"], ["ios"]]


def ydl_options(base: dict, cookies: dict, client) -> dict:
    o = dict(base)
    o.setdefault("socket_timeout", 30)
    o.setdefault("retries", 3)
    o.setdefault("extractor_retries", 2)
    if client:
        o["extractor_args"] = {"youtube": {"player_client": list(client)}}
    if cookies.get("file"):
        o["cookiefile"] = cookies["file"]
    elif cookies.get("browser"):
        o["cookiesfrombrowser"] = (cookies["browser"],)
    return o


def cookie_plans(cfg) -> list:
    cfg = cfg or {}
    f = cfg.get("file")
    if f and os.path.isfile(f):
        return [{"file": f}]
    b = cfg.get("browser")
    if b == "auto":
        return [{}] + [{"browser": x} for x in ("firefox", "chrome", "edge", "brave")]
    if b:
        return [{"browser": b}]
    return [{}]


def attempt_plans(cfg, prefer=None) -> list:
    plans = []
    for c in cookie_plans(cfg):
        clients = CLIENT_PLANS if not c else [None, ["tv"], ["web_safari"]]
        for cl in clients:
            plans.append((c, cl))
    if prefer is not None:
        plans = [p for p in plans if p != tuple(prefer)]
        plans.insert(0, tuple(prefer))
    return plans


def _describe(cookies: dict, client) -> str:
    parts = ["client=" + ",".join(client) if client else "default client"]
    if cookies.get("file"):
        parts.append("cookies.txt")
    elif cookies.get("browser"):
        parts.append(f"{cookies['browser']} cookies")
    return ", ".join(parts)


def ytdlp_version() -> str:
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return "unknown"


def update_ytdlp():
    cmd = [sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check", "yt-dlp"]
    kwargs = {"capture_output": True, "text": True, "timeout": 300}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # no console window
    try:
        r = subprocess.run(cmd, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "pip failed")[-400:]
    for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
        del sys.modules[name]
    globals()["yt_dlp"] = importlib.import_module("yt_dlp")
    return True, ytdlp_version()


def run_with_fallbacks(func, cfg, status, check, auto_update=True, prefer=None):
    # Tries func(cookies, client) over several cookie/client combinations; if all fail,
    # updates yt-dlp once and tries again. Returns (result, (cookies, client)).
    last = None
    updated = False
    while True:
        for idx, (cookies, client) in enumerate(attempt_plans(cfg, prefer)):
            check()
            if idx > 0:
                status(f"Trying another method ({_describe(cookies, client)})...")
            try:
                return func(cookies, client), (cookies, client)
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                check()
                last = exc
                text = str(exc).strip()
                first = text.splitlines()[0] if text else type(exc).__name__
                status(f"Failed ({_describe(cookies, client)}): {first[:200]}")
                if is_fatal(text):
                    raise
        if auto_update and not updated:
            updated = True
            status("All methods failed - updating yt-dlp and retrying once...")
            ok, info = update_ytdlp()
            if ok:
                status(f"yt-dlp is now version {info}.")
                continue
            status(f"Auto-update failed: {info[:160]}")
        break
    raise last


def parse_subtitle_file(path: str) -> List[Segment]:
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        raw = fh.read()
    if path.lower().endswith(".json3") or raw.lstrip().startswith("{"):
        return parse_json3(json.loads(raw))
    ts = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")
    segs: List[Segment] = []
    cur: Optional[Segment] = None
    for line in raw.splitlines():
        m = ts.search(line)
        if m:
            if cur and cur.text.strip():
                segs.append(cur)
            g = m.groups()
            s = int(g[0] or 0) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000
            e = int(g[4] or 0) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000
            cur = Segment(s, e, "")
        elif cur is not None:
            t = line.strip()
            if not t:
                if cur.text.strip():
                    segs.append(cur)
                cur = None
            else:
                cur.text += " " + re.sub(r"<[^>]+>", "", t)
    if cur and cur.text.strip():
        segs.append(cur)
    for s in segs:
        s.text = " ".join(html.unescape(s.text).split())
    segs = [s for s in segs if s.text]
    segs.sort(key=lambda s: s.start)
    return segs


def fetch_subs_via_ytdlp(url: str, ctx) -> List[Segment]:
    # yt-dlp's own subtitle downloader (uses the same cookies/client that worked for info).
    tmp = tempfile.mkdtemp(prefix="yt_subs_")
    try:
        cookies, client = ctx if ctx else ({}, None)
        opts = ydl_options({
            "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "writesubtitles": True, "writeautomaticsub": True,
            "subtitleslangs": ["en", "en-US", "en-GB", "en-orig"],
            "subtitlesformat": "json3/vtt/srt/best",
            "outtmpl": os.path.join(tmp, "sub.%(ext)s")}, cookies, client)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [f for f in os.listdir(tmp) if f.lower().endswith((".json3", ".vtt", ".srt"))]
        files.sort(key=lambda f: (".en." not in f, f))
        for f in files:
            try:
                segs = parse_subtitle_file(os.path.join(tmp, f))
                if segs:
                    return segs
            except Exception:
                pass
    except Exception:
        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return []


def fetch_transcript_fallback(video_id: str):
    # Optional third source: the youtube-transcript-api package (if installed).
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception:
        return [], ""
    langs = ["en", "en-US", "en-GB"]
    try:
        try:
            tr = YouTubeTranscriptApi().list(video_id).find_transcript(langs)
            items = [(s.text, s.start, s.duration) for s in tr.fetch()]
            kind = "auto" if getattr(tr, "is_generated", True) else "manual"
        except (TypeError, AttributeError):
            data = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
            items = [(d["text"], d["start"], d.get("duration", 0)) for d in data]
            kind = "auto"
        segs = [Segment(float(st), float(st) + float(du), " ".join(str(t).split()))
                for t, st, du in items if str(t).strip()]
        segs.sort(key=lambda s: s.start)
        return segs, kind
    except Exception:
        return [], ""


def make_local_info(path: str) -> VideoInfo:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("OpenCV could not open this video file (unsupported codec or corrupt file).")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    cap.release()
    vi = VideoInfo(source_url=path, title=os.path.splitext(os.path.basename(path))[0],
                   channel="Local file", duration=(n / fps if fps > 0 and n > 0 else 0.0),
                   is_local=True)
    base = os.path.splitext(path)[0]
    for suffix in (".en.srt", ".en.vtt", ".srt", ".vtt"):
        sidecar = base + suffix
        if os.path.isfile(sidecar):
            try:
                segs = parse_subtitle_file(sidecar)
            except Exception:
                segs = []
            if segs:
                vi.segments, vi.transcript_kind, vi.transcript_lang = segs, "file", "en"
                break
    return vi


def fetch_video_info(url: str, cfg=None, status=lambda m: None, check=lambda: None,
                     auto_update: bool = True) -> VideoInfo:
    def attempt(cookies, client):
        opts = ydl_options({"quiet": True, "no_warnings": True, "skip_download": True,
                            "noplaylist": True}, cookies, client)
        with yt_dlp.YoutubeDL(opts) as ydl:
            d = ydl.extract_info(url, download=False)
        if d and d.get("_type") == "playlist" and d.get("entries"):
            d = d["entries"][0]
        if not d:
            raise RuntimeError("No video information returned.")
        return d

    d, ctx = run_with_fallbacks(attempt, cfg, status, check, auto_update)
    if d.get("is_live"):
        raise RuntimeError("Live streams are not supported.")

    vi = VideoInfo(
        source_url=url,
        video_id=d.get("id", ""),
        title=d.get("title") or "Untitled video",
        channel=d.get("uploader") or d.get("channel") or "",
        duration=float(d.get("duration") or 0),
        ctx=ctx,
    )

    thumb = d.get("thumbnail")
    if thumb:
        try:
            req = urllib.request.Request(thumb, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                vi.thumbnail_bytes = r.read()
        except Exception:
            pass

    # Transcript source 1: caption URLs listed by yt-dlp
    for kind, table in (("manual", d.get("subtitles") or {}),
                        ("auto", d.get("automatic_captions") or {})):
        pick = _pick_english(table)
        if not pick:
            continue
        t_url, lang = pick
        try:
            req = urllib.request.Request(t_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                segs = parse_json3(json.loads(r.read().decode("utf-8")))
            if segs:
                vi.segments, vi.transcript_kind, vi.transcript_lang = segs, kind, lang
                break
        except Exception:
            continue

    # Transcript source 2: yt-dlp's own subtitle downloader (handles cookies / blocks better)
    if not vi.segments:
        check()
        status("Trying yt-dlp subtitle downloader...")
        segs = fetch_subs_via_ytdlp(url, ctx)
        if segs:
            vi.segments, vi.transcript_kind, vi.transcript_lang = segs, "captions", "en"

    # Transcript source 3: youtube-transcript-api (optional package)
    if not vi.segments and vi.video_id:
        check()
        status("Trying youtube-transcript-api fallback...")
        segs, kind = fetch_transcript_fallback(vi.video_id)
        if segs:
            vi.segments, vi.transcript_kind, vi.transcript_lang = segs, kind or "captions", "en"
    return vi


def fetch_playlist_info(url: str, cfg=None, status=lambda m: None, check=lambda: None,
                        auto_update: bool = True) -> PlaylistInfo:
    def attempt(cookies, client):
        opts = ydl_options({"quiet": True, "no_warnings": True, "skip_download": True,
                            "extract_flat": "in_playlist"}, cookies, client)
        with yt_dlp.YoutubeDL(opts) as ydl:
            d = ydl.extract_info(url, download=False)
        if not d:
            raise RuntimeError("No playlist information returned.")
        return d

    d, _ctx = run_with_fallbacks(attempt, cfg, status, check, auto_update)
    raw_entries = d.get("entries")
    if not raw_entries:
        raise RuntimeError("This link is a single video, not a playlist (or the playlist is empty/private).")
    entries = []
    for e in raw_entries:
        if not e:
            continue
        vid = e.get("id") or ""
        title = e.get("title") or vid
        if len(vid) != 11 or title in ("[Private video]", "[Deleted video]"):
            continue
        entries.append({"id": vid, "title": title, "duration": float(e.get("duration") or 0),
                        "url": f"https://www.youtube.com/watch?v={vid}"})
    if not entries:
        raise RuntimeError("The playlist has no accessible videos.")
    return PlaylistInfo(source_url=url, title=d.get("title") or "Playlist",
                        channel=d.get("uploader") or d.get("channel") or "", entries=entries)


# --------------------------------------------------------------------------- #
#  Transcript assignment + refinement (offline)
# --------------------------------------------------------------------------- #


def assign_transcript(times: List[float], segments: List[Segment]) -> List[List[str]]:
    """Each caption line goes to the last screenshot taken at/before its start.
    Lines before the first screenshot are attached to the first one. Because
    removed/duplicate screenshots simply aren't in `times`, their speech is
    automatically merged into the previous kept screenshot."""
    out: List[List[str]] = [[] for _ in times]
    if not times:
        return out
    for seg in segments:
        idx = max(bisect.bisect_right(times, seg.start) - 1, 0)
        out[idx].append(seg.text)
    return out


def merge_segments(texts: List[str]) -> str:
    """Join caption lines, removing the word overlap of rolling auto-captions."""
    words: List[str] = []
    for t in texts:
        w = html.unescape(t).split()
        if not w:
            continue
        k = 0
        for kk in range(min(len(words), len(w), 15), 1, -1):
            if [x.lower() for x in words[-kk:]] == [x.lower() for x in w[:kk]]:
                k = kk
                break
        words.extend(w[k:])
    return " ".join(words)


_TAGS = re.compile(
    r"[\[\(]\s*(?:music|applause|laughter|laughs|laughing|cheering|cheers|noise|"
    r"silence|inaudible|sighs?|singing|crowd|coughs?|clears throat|"
    r"background noise|foreign)\s*[\]\)]", re.I)
_FILLERS = re.compile(r"\b(?:u+m+|u+h+|e+r+m+|u+h+m+|h+m+|m+hm+)\b[,.]?\s*", re.I)
_STUTTER = re.compile(
    r"\b(i|the|a|an|and|to|of|in|it|is|that|we|you|so|but|this|my|your|they|he|she)"
    r"(?:\s+\1\b)+", re.I)
_BREAKERS = {"so", "but", "and", "now", "okay", "ok", "then", "because", "however",
             "next", "first", "second", "finally", "also", "alright", "well",
             "right", "anyway", "today", "let", "let's", "lets", "if", "when"}


def clean_text(text: str) -> str:
    t = html.unescape(text)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("\u266a", " ").replace("\u200b", "")
    t = _TAGS.sub(" ", t)
    t = re.sub(r"(?:^|\s)>>+\s*", " ", t)
    t = _FILLERS.sub("", t)
    t = _STUTTER.sub(r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)
    t = re.sub(r"([,;:])(\s*[,;:])+", r"\1", t)
    return t


def _fix_sentence(s: str) -> str:
    s = s.strip(" ,;:")
    if not s:
        return ""
    s = re.sub(r"\bi\b", "I", s)
    for i, ch in enumerate(s):
        if ch.isalpha():
            s = s[:i] + ch.upper() + s[i + 1:]
            break
    if s[-1] not in ".!?\u2026\"'":
        s += "."
    return s


def _chunk_sentences(words: List[str]) -> List[str]:
    sentences, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1].lower().strip(",.") if i + 1 < len(words) else None
        if nxt is not None and ((len(cur) >= 12 and nxt in _BREAKERS) or len(cur) >= 30):
            sentences.append(" ".join(cur))
            cur = []
    if cur:
        sentences.append(" ".join(cur))
    return sentences


def punctuate_paragraphs(clean: str) -> List[str]:
    words = clean.split()
    if not words:
        return []
    has_punct = len(re.findall(r"[.!?]", clean)) >= max(1, len(words) // 40)
    sentences = re.split(r"(?<=[.!?])\s+", clean) if has_punct else _chunk_sentences(words)
    sentences = [x for x in (_fix_sentence(s) for s in sentences) if x]
    paras, cur, count = [], [], 0
    for s in sentences:
        cur.append(s)
        count += len(s.split())
        if len(cur) >= 4 or count >= 80:
            paras.append(" ".join(cur))
            cur, count = [], 0
    if cur:
        paras.append(" ".join(cur))
    return paras


def refine_local(raw: str) -> List[str]:
    return punctuate_paragraphs(clean_text(raw))


def ai_refine(text: str, title: str, api_key: str, model: str) -> List[str]:
    system = (
        "You edit raw YouTube caption transcripts into clean, readable prose. "
        "Keep the speaker's meaning and wording as much as possible. Fix punctuation, "
        "capitalization and obvious mis-transcriptions (use the video title for "
        "context), remove filler words and false starts. Do NOT summarize, add facts, "
        "headings, bullet points or timestamps. Split into short paragraphs separated "
        "by blank lines. Output only the edited text.")
    body = {
        "model": model,
        "max_tokens": min(4000, max(300, len(text.split()) * 3)),
        "system": system,
        "messages": [{"role": "user", "content":
                      f"Video title: {title}\n\nTranscript excerpt (belongs under one "
                      f"screenshot):\n{text}"}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.load(r)
    out = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    paras = [p.strip() for p in re.split(r"\n\s*\n", out) if p.strip()]
    if not paras:
        raise RuntimeError("Empty response from API")
    return paras


def prepare_paragraphs(shots, segments, include, refine, ai, title, status, check,
                       progress=lambda f: None):
    # Returns (paragraphs_per_shot, ai) - ai becomes None if AI polishing was disabled on errors.
    n = len(shots)
    paragraphs: List[List[str]] = [[] for _ in range(n)]
    if not include or not segments or n == 0:
        return paragraphs, ai
    status("Preparing transcripts...")
    groups = assign_transcript([s.time for s in shots], segments)
    failures = 0
    for i, g in enumerate(groups):
        check()
        raw = merge_segments(g)
        if raw:
            if refine or ai:
                clean = clean_text(raw)
                paras = punctuate_paragraphs(clean)
                if ai and len(clean.split()) >= 6:
                    try:
                        status(f"Polishing transcript with Claude ({i + 1}/{n})...")
                        paras = ai_refine(clean, title, ai["key"], ai["model"])
                        failures = 0
                    except urllib.error.HTTPError as exc:
                        if exc.code in (401, 403):
                            status("API key rejected - using offline refinement instead.")
                            ai = None
                        else:
                            failures += 1
                            status(f"AI polish failed on section {i + 1} (HTTP {exc.code}); using offline refinement.")
                    except Exception as exc:  # noqa: BLE001
                        failures += 1
                        status(f"AI polish failed on section {i + 1} ({exc}); using offline refinement.")
                    if ai and failures >= 3:
                        status("Too many AI errors - switching to offline refinement.")
                        ai = None
                paragraphs[i] = paras
            else:
                paragraphs[i] = [html.unescape(raw)]
        progress((i + 1) / n)
    return paragraphs, ai


# --------------------------------------------------------------------------- #
#  Frame analysis
# --------------------------------------------------------------------------- #


def signature(frame):
    small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(gray, (3, 3), 0)


def changed_pct(a, b) -> float:
    return float(np.count_nonzero(cv2.absdiff(a, b) > 20)) / a.size * 100.0


def is_blank(sig) -> bool:
    return float(sig.mean()) < 10 or float(sig.std()) < 2.5


# --------------------------------------------------------------------------- #
#  PDF building
# --------------------------------------------------------------------------- #

_FONTS = None
_UNICODE = False


def setup_fonts():
    global _FONTS, _UNICODE
    if _FONTS:
        return _FONTS
    dj = "/usr/share/fonts/truetype/dejavu/"
    candidates = [
        ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
        ("/Library/Fonts/Arial Unicode.ttf", None),
        ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", None),
        (dj + "DejaVuSans.ttf", dj + "DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ]
    for reg, bold in candidates:
        if os.path.exists(reg):
            try:
                pdfmetrics.registerFont(TTFont("AppBody", reg))
                pdfmetrics.registerFont(TTFont("AppBold", bold if bold and os.path.exists(bold) else reg))
                _FONTS, _UNICODE = ("AppBody", "AppBold"), True
                return _FONTS
            except Exception:
                continue
    _FONTS, _UNICODE = ("Helvetica", "Helvetica-Bold"), False
    return _FONTS


def plain_safe(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"[\x00-\x08\x0b-\x1f]", "", s)
    if not _UNICODE:
        s = s.encode("cp1252", "ignore").decode("cp1252")
    return s


def xml_safe(s: str) -> str:
    return xml_escape(plain_safe(s))


def build_pdf(out_path, sections, page_size, new_page_each, progress=lambda p: None,
              doc_title=None, doc_author=None):
    # sections: list of {"meta": {...}, "shots": [Shot], "paragraphs": [[str]]}
    body_font, bold_font = setup_fonts()
    page_w, page_h = page_size
    lm = rm = 18 * mm
    tm, bm = 16 * mm, 18 * mm
    frame_w, frame_h = page_w - lm - rm, page_h - tm - bm

    s_title = ParagraphStyle("t", fontName=bold_font, fontSize=20, leading=25,
                             textColor=colors.HexColor("#111111"), alignment=TA_LEFT, spaceAfter=4)
    s_meta = ParagraphStyle("m", fontName=body_font, fontSize=9.5, leading=13,
                            textColor=colors.HexColor("#555555"), spaceAfter=2)
    s_cap = ParagraphStyle("c", fontName=bold_font, fontSize=9, leading=12,
                           textColor=colors.HexColor("#777777"), spaceAfter=3)
    s_body = ParagraphStyle("b", fontName=body_font, fontSize=10.5, leading=15.5,
                            textColor=colors.HexColor("#222222"), spaceAfter=5)

    story = []
    total = max(sum(len(s["shots"]) for s in sections), 1)
    done = 0
    for si, sec in enumerate(sections):
        meta, shots, paragraphs = sec["meta"], sec["shots"], sec["paragraphs"]
        if si > 0:
            story.append(PageBreak())
        if meta.get("tag"):
            story.append(Paragraph(xml_safe(meta["tag"]), s_meta))
        story.append(Paragraph(xml_safe(meta["title"]), s_title))
        if meta.get("channel"):
            story.append(Paragraph(xml_safe(meta["channel"]), s_meta))
        if meta.get("url"):
            u = xml_escape(meta["url"], {'"': "&quot;"})
            story.append(Paragraph(f'<link href="{u}" color="#1a56c4">{xml_escape(meta["url"])}</link>', s_meta))
        story.append(Spacer(1, 7 * mm))

        n = len(shots)
        if n == 0:
            story.append(Paragraph("No screenshots were captured for this video.", s_body))
        for i, shot in enumerate(shots):
            w = frame_w
            h = w * shot.h / shot.w
            max_h = frame_h * 0.55
            if h > max_h:
                h, w = max_h, max_h * shot.w / shot.h
            img = RLImage(io.BytesIO(shot.jpeg), width=w, height=h)
            tbl = Table([[img]], colWidths=[w])
            tbl.setStyle(TableStyle([
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#BBBBBB")),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
            block = [Paragraph(f"Screenshot {i + 1} of {n}", s_cap), tbl, Spacer(1, 4 * mm)]
            for para in paragraphs[i]:
                block.append(Paragraph(xml_safe(para), s_body))
            story.append(KeepTogether(block))
            if new_page_each and i < n - 1:
                story.append(PageBreak())
            else:
                story.append(Spacer(1, 6 * mm))
            done += 1
            progress(int(100 * done / total))

    first = sections[0]["meta"]
    title_for_doc = doc_title or first["title"]
    short_title = plain_safe(title_for_doc)[:70]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(body_font, 8)
        canvas.setFillColor(colors.HexColor("#888888"))
        canvas.drawCentredString(page_w / 2, 9 * mm, f"{short_title}   |   {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(out_path, pagesize=page_size, leftMargin=lm, rightMargin=rm,
                            topMargin=tm, bottomMargin=bm,
                            title=plain_safe(title_for_doc),
                            author=plain_safe(doc_author or first.get("channel", "")))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


# --------------------------------------------------------------------------- #
#  Background workers
# --------------------------------------------------------------------------- #


class Cancelled(Exception):
    pass


class BaseWorker(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def check(self):
        if self._cancel:
            raise Cancelled()

    def work(self):  # pragma: no cover
        raise NotImplementedError

    def run(self):
        try:
            self.work()
        except Cancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            if self._cancel:
                self.cancelled.emit()
            else:
                self.failed.emit(f"{type(exc).__name__}: {exc}")


class InfoWorker(BaseWorker):
    done = pyqtSignal(object)

    def __init__(self, url: str, cfg: dict, auto_update: bool):
        super().__init__()
        self.url, self.cfg, self.auto_update = url, cfg, auto_update

    def work(self):
        self.status.emit("Fetching video information and transcript...")
        self.done.emit(fetch_video_info(self.url, self.cfg, self.status.emit,
                                        self.check, self.auto_update))


class UpdateWorker(BaseWorker):
    done = pyqtSignal(str)

    def work(self):
        self.status.emit("Updating yt-dlp (pip install -U yt-dlp)...")
        ok, msg = update_ytdlp()
        if not ok:
            raise RuntimeError("Could not update yt-dlp automatically.\n"
                               "Run manually:  py -m pip install -U yt-dlp\n\n" + msg)
        self.done.emit(msg)


class CodecError(RuntimeError):
    pass


class VideoCapturer:
    # Plain (non-Qt) logic: download a video (or use a local file), sample frames, drop
    # duplicates / blanks, and hand each kept frame to on_shot(t, jpeg_bytes, w, h).
    # progress(frac) takes 0..1.
    def __init__(self, url, duration, height, interval, threshold, start, end,
                 max_shots, skip_blank, cfg, auto_update, local, prefer,
                 check, status, progress, on_shot):
        self.url, self.duration, self.height = url, duration, height
        self.interval, self.threshold = interval, threshold
        self.start_t, self.end_t = start, end
        self.max_shots, self.skip_blank = max_shots, skip_blank
        self.cfg, self.auto_update = cfg, auto_update
        self.local, self.prefer = local, prefer
        self.check, self.status, self.progress, self.on_shot = check, status, progress, on_shot
        self.force_avc = False

    def _download(self, tmp: str) -> str:
        h = self.height
        if self.force_avc:
            fmt = (f"bestvideo[height<={h}][vcodec^=avc1]/best[height<={h}][vcodec^=avc1]/"
                   f"bestvideo[vcodec^=avc1]/best[ext=mp4]/best")
        else:
            fmt = (f"bestvideo[height<={h}][vcodec^=avc1]/bestvideo[height<={h}][ext=mp4]/"
                   f"bestvideo[height<={h}]/best[height<={h}]/best/worst")

        def hook(d):
            self.check()
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    self.progress(0.35 * d.get("downloaded_bytes", 0) / total)

        def attempt(cookies, client):
            for f in os.listdir(tmp):
                try:
                    os.remove(os.path.join(tmp, f))
                except OSError:
                    pass
            opts = ydl_options({
                "quiet": True, "no_warnings": True, "noplaylist": True, "format": fmt,
                "outtmpl": os.path.join(tmp, "video.%(ext)s"), "progress_hooks": [hook],
                "noprogress": True, "retries": 5, "fragment_retries": 5,
                "concurrent_fragment_downloads": 4}, cookies, client)
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([self.url])
            files = [f for f in os.listdir(tmp)
                     if f.startswith("video.") and not f.endswith((".part", ".ytdl"))]
            if not files:
                raise RuntimeError("Video download produced no file.")
            return os.path.join(tmp, files[0])

        path, _ctx = run_with_fallbacks(attempt, self.cfg, self.status, self.check,
                                        self.auto_update, self.prefer)
        return path

    def _extract(self, path: str) -> int:
        cap = cv2.VideoCapture(path)
        try:
            if not cap.isOpened():
                raise CodecError("OpenCV could not open the video (codec unsupported?).")
            fps = cap.get(cv2.CAP_PROP_FPS) or 0
            nframes = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            dur = nframes / fps if fps > 0 and nframes > 0 else self.duration
            if dur <= 0:
                dur = self.duration
            start = max(self.start_t or 0.0, 0.0)
            end = min(self.end_t, dur) if self.end_t else dur
            if end <= start:
                raise RuntimeError("End time must be after start time.")
            span = end - start

            recent, kept, fails, t, any_ok = [], 0, 0, start, False
            while t <= end:
                self.check()
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
                ok, frame = cap.read()
                if not ok:
                    fails += 1
                    if fails >= 3:
                        break
                    t += self.interval
                    continue
                fails, any_ok = 0, True
                sig = signature(frame)
                skip = self.skip_blank and is_blank(sig)
                if not skip:
                    skip = any(changed_pct(sig, r) < self.threshold for r in recent)
                if not skip:
                    recent.append(sig)
                    recent[:] = recent[-6:]
                    ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
                    if ok2:
                        h, w = frame.shape[:2]
                        self.on_shot(t, buf.tobytes(), w, h)
                        kept += 1
                        if self.max_shots and kept >= self.max_shots:
                            break
                self.progress(0.35 + 0.65 * min((t - start) / span, 1.0))
                self.status(f"Scanning video... {fmt_duration(t)} / {fmt_duration(end)}  "
                            f"({kept} screenshots kept)")
                t += self.interval
            if not any_ok:
                raise CodecError("OpenCV opened the video but could not decode any frame.")
            return kept
        finally:
            cap.release()

    def run(self) -> int:
        tmp = tempfile.mkdtemp(prefix="yt_shots_")
        try:
            if self.local:
                self.progress(0.35)
                return self._extract(self.url)
            self.status("Downloading video stream...")
            path = self._download(tmp)
            self.check()
            try:
                return self._extract(path)
            except CodecError:
                self.status("Video codec not readable here - retrying with an H.264 stream...")
                self.force_avc = True
                path = self._download(tmp)
                self.check()
                return self._extract(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PlaylistInfoWorker(BaseWorker):
    done = pyqtSignal(object)

    def __init__(self, url: str, cfg: dict, auto_update: bool):
        super().__init__()
        self.url, self.cfg, self.auto_update = url, cfg, auto_update

    def work(self):
        self.status.emit("Reading playlist...")
        self.done.emit(fetch_playlist_info(self.url, self.cfg, self.status.emit,
                                           self.check, self.auto_update))


class CaptureWorker(BaseWorker):
    shot_ready = pyqtSignal(float, bytes, int, int)
    done = pyqtSignal(int)

    def __init__(self, url, duration, height, interval, threshold, start, end,
                 max_shots, skip_blank, cfg, auto_update, local=False, prefer=None):
        super().__init__()
        self.capturer = VideoCapturer(
            url, duration, height, interval, threshold, start, end, max_shots, skip_blank,
            cfg, auto_update, local, prefer, self.check, self.status.emit,
            lambda f: self.progress.emit(int(f * 100)), self.shot_ready.emit)

    def work(self):
        kept = self.capturer.run()
        self.progress.emit(100)
        self.done.emit(kept)


class ExportWorker(BaseWorker):
    done = pyqtSignal(str)

    def __init__(self, shots, segments, include, refine, ai, meta, page_size,
                 new_page, out_path):
        super().__init__()
        self.shots, self.segments, self.include = shots, segments, include
        self.refine, self.ai, self.meta = refine, ai, meta
        self.page_size, self.new_page, self.out_path = page_size, new_page, out_path

    def work(self):
        paragraphs, _ai = prepare_paragraphs(
            self.shots, self.segments, self.include, self.refine, self.ai, self.meta["title"],
            self.status.emit, self.check, lambda f: self.progress.emit(int(80 * f)))
        self.check()
        self.status.emit("Building PDF...")
        sec = {"meta": self.meta, "shots": self.shots, "paragraphs": paragraphs}
        build_pdf(self.out_path, [sec], self.page_size, self.new_page,
                  progress=lambda p: self.progress.emit(80 + int(p * 0.2)))
        self.progress.emit(100)
        self.done.emit(self.out_path)


class BatchWorker(BaseWorker):
    # Processes many videos (a playlist): capture -> transcript -> PDF, one after another.
    done = pyqtSignal(object)

    def __init__(self, entries, playlist_title, opts):
        super().__init__()
        self.entries, self.playlist_title, self.o = entries, playlist_title, opts

    def work(self):
        o = self.o
        n = len(self.entries)
        outputs, failures, sections = [], [], []
        ai = o["ai"]
        if o["separate"]:
            os.makedirs(o["out"], exist_ok=True)
        for i, e in enumerate(self.entries):
            self.check()
            self.status.emit(f"[{i + 1}/{n}] {e['title']}")

            def base(f, i=i):
                self.progress.emit(int(100 * (i + f) / n))

            local = e.get("local", False)
            try:
                if local:
                    vi = make_local_info(e["url"])
                else:
                    vi = fetch_video_info(e["url"], o["cfg"], self.status.emit, self.check,
                                          o["auto_update"])
                shots: List[Shot] = []
                cap = VideoCapturer(
                    e["url"], vi.duration or e.get("duration", 0), o["height"], o["interval"],
                    o["threshold"], None, None, o["max_shots"], o["skip_blank"], o["cfg"],
                    o["auto_update"], local, None if local else vi.ctx, self.check, self.status.emit,
                    lambda f, b=base: b(0.8 * f),
                    lambda t, j, w, h, s=shots: s.append(Shot(t, j, w, h)))
                cap.run()
                include = o["include"] and bool(vi.segments)
                if o["include"] and not vi.segments:
                    self.status.emit("No English transcript for this video - screenshots only.")
                paragraphs, ai = prepare_paragraphs(
                    shots, vi.segments, include, o["refine"], ai, vi.title, self.status.emit,
                    self.check, lambda f, b=base: b(0.8 + 0.15 * f))
                meta = {"title": vi.title, "channel": vi.channel, "url": e["url"],
                        "tag": "" if o["separate"] else f"Video {i + 1} of {n}"}
                sec = {"meta": meta, "shots": shots, "paragraphs": paragraphs}
                if o["separate"]:
                    path = os.path.join(o["out"], f"{i + 1:02d} - {safe_filename(vi.title)}.pdf")
                    build_pdf(path, [sec], o["page_size"], o["new_page"])
                    outputs.append(path)
                else:
                    sections.append(sec)
                base(1.0)
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                if self._cancel:
                    raise Cancelled() from None
                text = str(exc).strip()
                reason = (text.splitlines()[0] if text else type(exc).__name__)[:200]
                failures.append((e["title"], reason))
                self.status.emit(f"Skipped '{e['title']}': {reason}")
            if not local:
                QThread.msleep(500)  # be polite to YouTube between videos

        if not o["separate"] and sections:
            self.status.emit("Building combined PDF...")
            build_pdf(o["out"], sections, o["page_size"], o["new_page"],
                      doc_title=self.playlist_title)
            outputs.append(o["out"])
        self.progress.emit(100)
        self.done.emit({"outputs": outputs, "failed": failures,
                        "separate": o["separate"], "out": o["out"]})


# --------------------------------------------------------------------------- #
#  Main window
# --------------------------------------------------------------------------- #

PAGE_SIZES = {"A4 (portrait)": A4, "US Letter (portrait)": letter,
              "A4 (landscape)": landscape(A4)}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YouTube -> Screenshot PDF Studio")
        self.resize(1300, 820)
        self.info: Optional[VideoInfo] = None
        self.worker: Optional[BaseWorker] = None
        self._pending_capture = False
        self._pending_batch = False
        self._out_auto = True
        self._out_dir: Optional[str] = None
        self._list_source: Optional[str] = None
        self.playlist: Optional[PlaylistInfo] = None
        self._threads: list = []
        self._build_ui()
        self._sync_transcript_controls()

    # ---- UI construction ------------------------------------------------- #
    def _build_ui(self):
        # URL row
        self.ed_url = QLineEdit()
        self.ed_url.setPlaceholderText("Paste a YouTube link (video URL)...")
        self.ed_url.returnPressed.connect(self.on_fetch)
        self.ed_url.textChanged.connect(self._url_changed)
        btn_paste = QPushButton("Paste")
        btn_paste.clicked.connect(lambda: self.ed_url.setText(QApplication.clipboard().text().strip()))
        self.btn_fetch = QPushButton("Fetch info")
        self.btn_fetch.clicked.connect(self.on_fetch)
        url_row = QHBoxLayout()
        url_row.addWidget(self.ed_url, 1)
        url_row.addWidget(btn_paste)
        url_row.addWidget(self.btn_fetch)
        self.btn_local = QPushButton("Open local video...")
        self.btn_local.setToolTip("Use a video file already on your PC (always works, no YouTube needed).")
        self.btn_local.clicked.connect(self.on_open_local)
        self.btn_local_multi = QPushButton("Open multiple local videos...")
        self.btn_local_multi.setToolTip(
            "Pick several video files already on your PC and process them all - one PDF per "
            "video or one combined PDF. A same-named .srt/.vtt next to a file is used as its transcript.")
        self.btn_local_multi.clicked.connect(self.on_open_local_batch)
        self.btn_trfile = QPushButton("Load transcript file...")
        self.btn_trfile.setToolTip("Attach a .srt / .vtt / .json3 transcript to the current video.")
        self.btn_trfile.clicked.connect(self.on_load_transcript)
        url_row2 = QHBoxLayout()
        url_row2.addWidget(self.btn_local)
        url_row2.addWidget(self.btn_local_multi)
        url_row2.addWidget(self.btn_trfile)
        url_row2.addStretch(1)
        self.chk_playlist = QCheckBox("Batch mode (YouTube playlist or multiple local videos)")
        self.chk_playlist.setToolTip(
            "Paste a playlist link and click Fetch info, or use 'Open multiple local videos...' "
            "above, then pick which videos to process.")
        self.chk_playlist.toggled.connect(self._apply_mode)
        btn_clear = QPushButton("Clear / new video")
        btn_clear.clicked.connect(self.on_reset)
        url_row3 = QHBoxLayout()
        url_row3.addWidget(self.chk_playlist)
        url_row3.addStretch(1)
        url_row3.addWidget(btn_clear)

        # Info group
        self.lbl_thumb = QLabel()
        self.lbl_thumb.setFixedSize(160, 90)
        self.lbl_thumb.setStyleSheet("background:#222;border-radius:4px;")
        self.lbl_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_title = QLabel("No video loaded")
        self.lbl_title.setWordWrap(True)
        self.lbl_title.setStyleSheet("font-weight:600;")
        self.lbl_meta = QLabel("")
        self.lbl_meta.setStyleSheet("color:#666;")
        self.lbl_tr = QLabel("")
        self.lbl_tr.setWordWrap(True)
        info_text = QVBoxLayout()
        info_text.addWidget(self.lbl_title)
        info_text.addWidget(self.lbl_meta)
        info_text.addWidget(self.lbl_tr)
        info_text.addStretch(1)
        info_row = QHBoxLayout()
        info_row.addWidget(self.lbl_thumb)
        info_row.addLayout(info_text, 1)
        g_info = QGroupBox("Video")
        g_info.setLayout(info_row)

        # Connection & reliability
        self.cb_cookies = QComboBox()
        self.cb_cookies.addItem("None", None)
        self.cb_cookies.addItem("Auto (try browsers if blocked)", "auto")
        for label, key in (("Firefox", "firefox"), ("Chrome", "chrome"), ("Edge", "edge"),
                           ("Brave", "brave"), ("Opera", "opera"), ("Vivaldi", "vivaldi")):
            self.cb_cookies.addItem(label, key)
        self.cb_cookies.setToolTip("Use your logged-in YouTube session to get past bot checks, "
                                   "age limits, etc.\nFirefox is the most reliable on Windows.")
        self.ed_cookiefile = QLineEdit()
        self.ed_cookiefile.setPlaceholderText("cookies.txt (optional, overrides browser)")
        btn_ck = QPushButton("Browse...")
        btn_ck.clicked.connect(self.on_browse_cookies)
        ck_row = QHBoxLayout()
        ck_row.addWidget(self.ed_cookiefile, 1)
        ck_row.addWidget(btn_ck)
        self.chk_update = QCheckBox("Auto-update yt-dlp if everything fails")
        self.chk_update.setChecked(True)
        self.lbl_ver = QLabel(f"yt-dlp {ytdlp_version()}")
        self.lbl_ver.setStyleSheet("color:#666;")
        self.btn_upd = QPushButton("Update yt-dlp now")
        self.btn_upd.clicked.connect(self.on_update_ytdlp)
        upd_row = QHBoxLayout()
        upd_row.addWidget(self.lbl_ver, 1)
        upd_row.addWidget(self.btn_upd)
        f0 = QFormLayout()
        f0.addRow("Browser cookies", self.cb_cookies)
        f0.addRow("cookies.txt", ck_row)
        f0.addRow("", self.chk_update)
        f0.addRow("", upd_row)
        g_net = QGroupBox("Connection && reliability")
        g_net.setLayout(f0)

        # Capture settings
        self.sp_interval = QDoubleSpinBox()
        self.sp_interval.setRange(0.5, 300)
        self.sp_interval.setValue(5.0)
        self.sp_interval.setSuffix(" s")
        self.sp_interval.setToolTip("How often the video is sampled. Smaller = more candidates (slower).")
        self.sp_thresh = QDoubleSpinBox()
        self.sp_thresh.setRange(0.1, 50)
        self.sp_thresh.setValue(2.0)
        self.sp_thresh.setSuffix(" %")
        self.sp_thresh.setToolTip("A frame is kept only if more than this % of its pixels differ\n"
                                  "from the recent kept screenshots. Higher = fewer screenshots.")
        self.cb_quality = QComboBox()
        for q in (360, 480, 720, 1080):
            self.cb_quality.addItem(f"{q}p", q)
        self.cb_quality.setCurrentIndex(2)
        self.ed_start = QLineEdit()
        self.ed_start.setPlaceholderText("start (mm:ss) - optional")
        self.ed_end = QLineEdit()
        self.ed_end.setPlaceholderText("end (mm:ss) - optional")
        rng = QHBoxLayout()
        rng.addWidget(self.ed_start)
        rng.addWidget(self.ed_end)
        self.sp_max = QSpinBox()
        self.sp_max.setRange(0, 5000)
        self.sp_max.setSpecialValueText("unlimited")
        self.sp_max.setValue(0)
        self.chk_blank = QCheckBox("Skip black / blank frames")
        self.chk_blank.setChecked(True)
        f1 = QFormLayout()
        f1.addRow("Sample every", self.sp_interval)
        f1.addRow("Min. change for new shot", self.sp_thresh)
        f1.addRow("Video quality", self.cb_quality)
        f1.addRow("Time range", rng)
        f1.addRow("Max screenshots", self.sp_max)
        f1.addRow("", self.chk_blank)
        g_cap = QGroupBox("Screenshot capture")
        g_cap.setLayout(f1)

        # Transcript settings
        self.chk_tr = QCheckBox("Include transcript under each screenshot")
        self.chk_tr.toggled.connect(self._sync_transcript_controls)
        self.chk_refine = QCheckBox("Refine into readable paragraphs (offline)")
        self.chk_refine.setChecked(True)
        self.chk_refine.toggled.connect(self.update_detail)
        self.chk_ai = QCheckBox("Polish with Claude (optional, needs API key)")
        self.chk_ai.toggled.connect(self._sync_transcript_controls)
        self.ed_key = QLineEdit(os.environ.get("ANTHROPIC_API_KEY", ""))
        self.ed_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_key.setPlaceholderText("Anthropic API key (not saved)")
        self.ed_model = QLineEdit("claude-sonnet-5-5")
        self.ed_model.setPlaceholderText("model name")
        v2 = QVBoxLayout()
        v2.addWidget(self.chk_tr)
        v2.addWidget(self.chk_refine)
        v2.addWidget(self.chk_ai)
        v2.addWidget(self.ed_key)
        v2.addWidget(self.ed_model)
        g_tr = QGroupBox("Transcript")
        g_tr.setLayout(v2)

        # PDF settings
        self.cb_page = QComboBox()
        self.cb_page.addItems(list(PAGE_SIZES))
        self.chk_newpage = QCheckBox("Start every screenshot on a new page")
        self.ed_out = QLineEdit()
        self.ed_out.setPlaceholderText("Output PDF path")
        self.ed_out.textEdited.connect(lambda _: setattr(self, "_out_auto", False))
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self.on_browse)
        out_row = QHBoxLayout()
        out_row.addWidget(self.ed_out, 1)
        out_row.addWidget(btn_browse)
        f3 = QFormLayout()
        f3.addRow("Page size", self.cb_page)
        f3.addRow("", self.chk_newpage)
        f3.addRow("Save as", out_row)
        self.cb_plmode = QComboBox()
        self.cb_plmode.addItem("One PDF per video (into a folder)", "separate")
        self.cb_plmode.addItem("One combined PDF", "combined")
        self.cb_plmode.currentIndexChanged.connect(self._plmode_changed)
        self.lbl_plmode = QLabel("Playlist output")
        f3.addRow(self.lbl_plmode, self.cb_plmode)
        g_pdf = QGroupBox("PDF")
        g_pdf.setLayout(f3)

        # Actions
        self.btn_capture = QPushButton("1. Capture screenshots")
        self.btn_capture.clicked.connect(self.on_capture)
        self.btn_export = QPushButton("2. Export PDF")
        self.btn_export.clicked.connect(self.on_export)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.on_cancel)
        for b in (self.btn_capture, self.btn_export):
            b.setMinimumHeight(34)
            b.setStyleSheet("font-weight:600;")
        act = QHBoxLayout()
        act.addWidget(self.btn_capture, 1)
        act.addWidget(self.btn_export, 1)
        self.btn_batch = QPushButton("Process whole playlist -> PDF(s)")
        self.btn_batch.setMinimumHeight(34)
        self.btn_batch.setStyleSheet("font-weight:600;")
        self.btn_batch.clicked.connect(self.on_batch)
        act.addWidget(self.btn_batch, 2)
        act.addWidget(self.btn_cancel)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.lbl_status = QLabel("Ready.")
        self.lbl_status.setWordWrap(True)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(90)
        self.log.setMaximumBlockCount(500)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addLayout(url_row)
        lv.addLayout(url_row2)
        lv.addLayout(url_row3)
        for w in (g_info, g_net, g_cap, g_tr, g_pdf):
            lv.addWidget(w)
        lv.addLayout(act)
        lv.addWidget(self.bar)
        lv.addWidget(self.lbl_status)
        lv.addWidget(self.log)
        lv.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left)
        scroll.setMinimumWidth(460)
        scroll.setMaximumWidth(560)

        # Right: review area
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setIconSize(QSize(176, 99))
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setSpacing(6)
        self.list.setWordWrap(True)
        self.list.currentItemChanged.connect(lambda *_: self.update_detail())
        self.list.itemChanged.connect(lambda *_: self.update_detail())
        self.list.itemDoubleClicked.connect(self._toggle_item)

        self.lbl_count = QLabel("Captured screenshots: 0  (untick to drop one; double-click toggles)")
        b_all = QPushButton("Select all")
        b_none = QPushButton("Select none")
        b_del = QPushButton("Delete unchecked")
        b_all.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        b_none.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        b_del.clicked.connect(self.on_delete_unchecked)
        top = QHBoxLayout()
        top.addWidget(self.lbl_count, 1)
        for b in (b_all, b_none, b_del):
            top.addWidget(b)

        self.preview = QLabel("Select a screenshot to preview it here")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(320, 180)
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.preview.setStyleSheet("background:#1b1b1b;color:#aaa;border-radius:4px;")
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("Transcript for the selected screenshot appears here.")
        detail_split = QSplitter(Qt.Orientation.Horizontal)
        detail_split.addWidget(self.preview)
        detail_split.addWidget(self.detail)
        detail_split.setSizes([520, 380])

        list_box = QWidget()
        lb = QVBoxLayout(list_box)
        lb.setContentsMargins(0, 0, 0, 0)
        lb.addLayout(top)
        lb.addWidget(self.list, 1)
        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(list_box)
        right.addWidget(detail_split)
        right.setSizes([420, 360])

        self.pl_list = QListWidget()
        self.pl_label = QLabel("Paste a playlist link, tick 'Entire playlist', then click 'Fetch info'.")
        self.pl_label.setWordWrap(True)
        pb_all = QPushButton("Select all")
        pb_none = QPushButton("Select none")
        pb_all.clicked.connect(lambda: self._pl_set_all(Qt.CheckState.Checked))
        pb_none.clicked.connect(lambda: self._pl_set_all(Qt.CheckState.Unchecked))
        self.pl_list.itemChanged.connect(lambda *_: self._pl_count())
        ptop = QHBoxLayout()
        ptop.addWidget(self.pl_label, 1)
        ptop.addWidget(pb_all)
        ptop.addWidget(pb_none)
        pl_page = QWidget()
        pv = QVBoxLayout(pl_page)
        pv.addLayout(ptop)
        pv.addWidget(self.pl_list, 1)
        self.tabs = QTabWidget()
        self.tabs.addTab(right, "Screenshots (review)")
        self.tabs.addTab(pl_page, "Playlist")
        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(scroll)
        main.addWidget(self.tabs)
        main.setStretchFactor(1, 1)
        self.setCentralWidget(main)
        self._refresh_mode_controls()

    # ---- State helpers --------------------------------------------------- #
    def _url_changed(self):
        text = self.ed_url.text().strip()
        if re.search(r"youtube\.com/playlist\?[^#\s]*list=", text) and not self.chk_playlist.isChecked():
            self.chk_playlist.setChecked(True)  # also resets state via _apply_mode
        if self.info and self.info.source_url != normalize_url(text):
            self.info = None
        if self.playlist and self.playlist.source_url != normalize_url(text, keep_playlist=True):
            self.playlist = None
        self._sync_transcript_controls()

    def _sync_transcript_controls(self, *_):
        has = bool(self.info and self.info.segments) or self.playlist is not None
        self.chk_tr.setEnabled(has)
        if not has:
            self.chk_tr.setChecked(False)
        on = has and self.chk_tr.isChecked()
        self.chk_refine.setEnabled(on)
        self.chk_ai.setEnabled(on)
        ai_on = on and self.chk_ai.isChecked()
        self.ed_key.setEnabled(ai_on)
        self.ed_model.setEnabled(ai_on)
        self.update_detail()

    def set_busy(self, busy: bool):
        for w in (self.btn_fetch, self.btn_capture, self.btn_export, self.ed_url,
                  self.btn_local, self.btn_local_multi, self.btn_trfile, self.btn_upd, self.btn_batch,
                  self.chk_playlist):
            w.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)

    def say(self, msg: str):
        self.lbl_status.setText(msg)
        if not msg.startswith("Scanning video"):  # don't spam the log
            self.log.appendPlainText(msg)

    def _start(self, worker: BaseWorker):
        self.worker = worker
        self._threads.append(worker)
        worker.finished.connect(self._reap)
        worker.progress.connect(self.bar.setValue)
        worker.status.connect(self.say)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        self.bar.setValue(0)
        self.set_busy(True)
        worker.start()

    def _end_job(self):
        self.worker = None
        self.set_busy(False)
        self._refresh_mode_controls()
        self._sync_transcript_controls()

    def _on_failed(self, msg: str):
        self._pending_capture = False
        self._pending_batch = False
        self._end_job()
        hint = explain_error(msg)
        self.say(f"Error: {msg}")
        self.log.appendPlainText("What to try: " + hint)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Something went wrong")
        box.setText(msg[:700])
        box.setInformativeText(hint)
        box.exec()

    def _on_cancelled(self):
        self._pending_capture = False
        self._pending_batch = False
        self._end_job()
        self.say("Cancelled.")

    def on_cancel(self):
        if self.worker:
            self.worker.cancel()
            self.say("Cancelling...")

    # ---- Step 0: fetch info --------------------------------------------- #
    def _net_cfg(self) -> dict:
        return {"browser": self.cb_cookies.currentData(),
                "file": self.ed_cookiefile.text().strip().strip('"')}

    def on_browse_cookies(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select cookies.txt", "",
                                              "Cookie files (*.txt);;All files (*)")
        if path:
            self.ed_cookiefile.setText(path)

    def on_update_ytdlp(self):
        w = UpdateWorker()
        w.done.connect(self._on_updated)
        self._start(w)

    def _on_updated(self, ver: str):
        self.lbl_ver.setText(f"yt-dlp {ver}")
        self._end_job()
        self.say(f"yt-dlp is up to date ({ver}).")

    def on_open_local(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open a video file", "",
            "Video files (*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.flv *.wmv);;All files (*)")
        if path:
            self.ed_url.setText(path)
            self.on_fetch()

    def on_open_local_batch(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open video files", "",
            "Video files (*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.flv *.wmv);;All files (*)")
        if not paths:
            return
        if not self.chk_playlist.isChecked():
            self.chk_playlist.setChecked(True)  # switches the UI into batch mode (resets self.playlist)
        entries = []
        for p in paths:
            cap = cv2.VideoCapture(p)
            dur = 0.0
            if cap.isOpened():
                fps = cap.get(cv2.CAP_PROP_FPS) or 0
                n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                dur = n / fps if fps > 0 and n > 0 else 0.0
            cap.release()
            entries.append({"id": p, "title": os.path.splitext(os.path.basename(p))[0],
                            "duration": dur, "url": p, "local": True})
        same_dir = len({os.path.dirname(e["url"]) for e in entries}) == 1
        title = (os.path.basename(os.path.dirname(entries[0]["url"])) if same_dir
                else f"{len(entries)} local videos")
        pl = PlaylistInfo(source_url="local-batch", title=title or "Local videos",
                          channel="Local files", entries=entries, is_local=True)
        self._on_playlist(pl)

    def on_load_transcript(self):
        if not self.info:
            QMessageBox.information(self, "Transcript file",
                                    "Fetch a YouTube video (or open a local video) first.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Load transcript", "",
                                              "Subtitles (*.srt *.vtt *.json3);;All files (*)")
        if not path:
            return
        try:
            segs = parse_subtitle_file(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Transcript file", f"Could not read the file:\n{exc}")
            return
        if not segs:
            QMessageBox.warning(self, "Transcript file",
                                "No timed captions found. Use a .srt, .vtt or .json3 file.")
            return
        self.info.segments, self.info.transcript_kind = segs, "file"
        self.lbl_tr.setText(f"Transcript loaded from file ({len(segs)} lines).")
        self.lbl_tr.setStyleSheet("color:#1a7f37;")
        self.chk_tr.setEnabled(True)
        self.chk_tr.setChecked(True)
        self._sync_transcript_controls()
        self.say("Transcript file loaded.")

    # ---- Step 0: fetch info --------------------------------------------- #
    def _set_url_text(self, url: str):
        if url != self.ed_url.text().strip():
            self.ed_url.blockSignals(True)
            self.ed_url.setText(url)
            self.ed_url.blockSignals(False)

    def _default_out(self) -> str:
        base = self._out_dir or os.path.expanduser("~/Documents")
        if not os.path.isdir(base):
            base = os.path.expanduser("~")
        if self.playlist:
            name = safe_filename(self.playlist.title)
            if self.playlist.is_local:
                base = os.path.dirname(self.playlist.entries[0]["url"]) or base
            if self.cb_plmode.currentData() == "separate":
                return os.path.join(base, name)
            return unique_path(os.path.join(base, name + ".pdf"))
        if self.info:
            return unique_path(os.path.join(base, safe_filename(self.info.title) + ".pdf"))
        return ""

    def _refresh_mode_controls(self):
        pl = self.chk_playlist.isChecked()
        self.btn_batch.setVisible(pl)
        self.btn_capture.setVisible(not pl)
        self.btn_export.setVisible(not pl)
        self.lbl_plmode.setVisible(pl)
        self.cb_plmode.setVisible(pl)
        self.ed_start.setEnabled(not pl)
        self.ed_end.setEnabled(not pl)
        if pl:
            self.btn_local.setEnabled(False)
            self.btn_trfile.setEnabled(False)

    def _apply_mode(self, *_):
        self.info = None
        self.playlist = None
        self._pending_capture = self._pending_batch = False
        self._refresh_mode_controls()
        if self.worker is None:
            self.btn_local.setEnabled(not self.chk_playlist.isChecked())
            self.btn_trfile.setEnabled(not self.chk_playlist.isChecked())
            self.btn_local_multi.setEnabled(True)
        self.tabs.setCurrentIndex(1 if self.chk_playlist.isChecked() else 0)
        self._out_auto = True
        self._sync_transcript_controls()

    def _plmode_changed(self, *_):
        self._out_auto = True
        if self.playlist:
            self.ed_out.setText(self._default_out())

    def _reap(self):
        self._threads = [t for t in self._threads if not t.isFinished()]

    def on_reset(self):
        if self.worker is not None:
            QMessageBox.information(self, "Busy", "Please wait for the current job to finish, or click Cancel.")
            return
        self.ed_url.blockSignals(True)
        self.ed_url.clear()
        self.ed_url.blockSignals(False)
        self.info = None
        self.playlist = None
        self._pending_capture = self._pending_batch = False
        self.list.clear()
        self._list_source = None
        self.pl_list.clear()
        self._pl_count()
        self.lbl_thumb.clear()
        self.lbl_title.setText("No video loaded")
        self.lbl_meta.clear()
        self.lbl_tr.clear()
        self.ed_out.clear()
        self._out_auto = True
        self.bar.setValue(0)
        self.update_detail()
        self._sync_transcript_controls()
        self.say("Ready for a new video.")

    def on_uncaught(self, etype, value, tb):
        import traceback
        text = "".join(traceback.format_exception(etype, value, tb))
        sys.stderr.write(text)
        self.log.appendPlainText(text)
        self.say(f"Unexpected error: {value}")
        if not (self.worker and self.worker.isRunning()):
            self.worker = None
            self.set_busy(False)
            self._refresh_mode_controls()
        QMessageBox.warning(self, "Unexpected error",
                            f"{etype.__name__}: {value}\n\nDetails were added to the log box. "
                            "The app is still running - you can try again.")

    # ---- Step 0: fetch info --------------------------------------------- #
    def on_fetch(self):
        raw = self.ed_url.text().strip()
        if not raw:
            QMessageBox.information(self, "YouTube link", "Please paste a YouTube link first.")
            self._pending_capture = self._pending_batch = False
            return
        if self.chk_playlist.isChecked():
            if not playlist_id(raw):
                QMessageBox.information(
                    self, "Playlist",
                    "That link has no playlist in it (no 'list=' part).\n"
                    "Paste a playlist link, or untick 'Entire playlist'.")
                self._pending_capture = self._pending_batch = False
                return
            url = normalize_url(raw, keep_playlist=True)
            self._set_url_text(url)
            w = PlaylistInfoWorker(url, self._net_cfg(), self.chk_update.isChecked())
            w.done.connect(self._on_playlist)
            self._start(w)
            return
        url = normalize_url(raw)
        self._set_url_text(url)
        if os.path.isfile(url):
            try:
                vi = make_local_info(url)
            except Exception as exc:  # noqa: BLE001
                self._pending_capture = False
                QMessageBox.critical(self, "Local video", str(exc))
                return
            self._on_info(vi)
            return
        w = InfoWorker(url, self._net_cfg(), self.chk_update.isChecked())
        w.done.connect(self._on_info)
        self._start(w)

    def _pl_set_all(self, state):
        self.pl_list.blockSignals(True)
        for i in range(self.pl_list.count()):
            self.pl_list.item(i).setCheckState(state)
        self.pl_list.blockSignals(False)
        self._pl_count()

    def _pl_entries(self) -> list:
        return [self.pl_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.pl_list.count())
                if self.pl_list.item(i).checkState() == Qt.CheckState.Checked]

    def _pl_count(self):
        total = self.pl_list.count()
        if self.playlist:
            self.pl_label.setText(f"{self.playlist.title}  -  {len(self._pl_entries())} of {total} "
                                  f"videos selected")
        elif total == 0:
            self.pl_label.setText("Paste a playlist link, tick 'Entire playlist', then click 'Fetch info'.")

    def _on_playlist(self, pl: PlaylistInfo):
        self.playlist = pl
        self.info = None
        self.lbl_thumb.clear()
        self.lbl_title.setText(pl.title)
        self.lbl_meta.setText(f"{pl.channel}  -  {len(pl.entries)} videos" if pl.channel
                              else f"{len(pl.entries)} videos")
        self.lbl_tr.setText("Transcripts are fetched per video while processing.")
        self.lbl_tr.setStyleSheet("color:#666;")
        self.pl_list.blockSignals(True)
        self.pl_list.clear()
        for i, e in enumerate(pl.entries, 1):
            dur = f"  ({fmt_duration(e['duration'])})" if e.get("duration") else ""
            it = QListWidgetItem(f"{i}. {e['title']}{dur}")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            it.setData(Qt.ItemDataRole.UserRole, e)
            self.pl_list.addItem(it)
        self.pl_list.blockSignals(False)
        self._pl_count()
        self.tabs.setCurrentIndex(1)
        pending, self._pending_batch = self._pending_batch, False
        self._end_job()
        self.chk_tr.setChecked(True)
        if self._out_auto or not self.ed_out.text().strip():
            self.ed_out.setText(self._default_out())
        self.say(f"Playlist loaded: {len(pl.entries)} videos. Untick any you don't want, then click "
                 f"'Process whole playlist'.")
        if pending:
            self.on_batch()

    # ---- Playlist batch --------------------------------------------------- #
    def on_batch(self):
        if self.playlist and self.playlist.is_local:
            pass  # nothing to fetch - the file list was already built locally
        else:
            raw = self.ed_url.text().strip()
            if not raw:
                QMessageBox.information(self, "Playlist", "Please paste a playlist link first.")
                return
            if not self.playlist or self.playlist.source_url != normalize_url(raw, keep_playlist=True):
                self._pending_batch = True
                self.on_fetch()
                return
        entries = self._pl_entries()
        if not entries:
            QMessageBox.information(self, "Batch",
                                    "Tick at least one video in the Playlist tab, or use "
                                    "'Open multiple local videos...' to pick some.")
            return
        include = self.chk_tr.isChecked()
        ai = None
        if include and self.chk_ai.isChecked():
            key, model = self.ed_key.text().strip(), self.ed_model.text().strip()
            if not key or not model:
                QMessageBox.warning(self, "Claude polish", "Enter an API key and model name, "
                                    "or untick 'Polish with Claude'.")
                return
            ai = {"key": key, "model": model}
        separate = self.cb_plmode.currentData() == "separate"
        if not separate and len(entries) > 30 and QMessageBox.question(
                self, "Large combined PDF",
                f"A combined PDF of {len(entries)} videos keeps every screenshot in memory and can "
                "get very large.\nContinue? (One PDF per video is lighter.)"
        ) != QMessageBox.StandardButton.Yes:
            return
        out = self.ed_out.text().strip() or self._default_out()
        if not separate and not out.lower().endswith(".pdf"):
            out += ".pdf"
        self.ed_out.setText(out)
        opts = {
            "height": self.cb_quality.currentData(), "interval": self.sp_interval.value(),
            "threshold": self.sp_thresh.value(), "max_shots": self.sp_max.value(),
            "skip_blank": self.chk_blank.isChecked(), "cfg": self._net_cfg(),
            "auto_update": self.chk_update.isChecked(), "include": include,
            "refine": self.chk_refine.isChecked(), "ai": ai,
            "page_size": PAGE_SIZES[self.cb_page.currentText()],
            "new_page": self.chk_newpage.isChecked(), "separate": separate, "out": out}
        w = BatchWorker(entries, self.playlist.title, opts)
        w.done.connect(self._on_batch_done)
        self._start(w)

    def _on_batch_done(self, res: dict):
        self._end_job()
        outs, failed = res["outputs"], res["failed"]
        folder = res["out"] if res["separate"] else os.path.dirname(os.path.abspath(res["out"]))
        self._out_auto = True
        self._out_dir = os.path.dirname(folder) if res["separate"] else folder
        if res["separate"]:
            msg = f"Created {len(outs)} PDF file(s) in:\n{folder}"
        else:
            msg = f"Created combined PDF:\n{res['out']}" if outs else "No PDF was created."
        if failed:
            lines = [f"- {t[:60]}: {m[:90]}" for t, m in failed[:8]]
            if len(failed) > 8:
                lines.append(f"...and {len(failed) - 8} more")
            msg += f"\n\n{len(failed)} video(s) were skipped:\n" + "\n".join(lines)
        self.say(msg.splitlines()[0])
        box = QMessageBox(self)
        box.setWindowTitle("Playlist finished")
        box.setText(msg)
        b_dir = box.addButton("Open folder", QMessageBox.ButtonRole.AcceptRole)
        b_pdf = None
        if outs and not res["separate"]:
            b_pdf = box.addButton("Open PDF", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is b_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        elif b_pdf is not None and box.clickedButton() is b_pdf:
            QDesktopServices.openUrl(QUrl.fromLocalFile(res["out"]))

    def _on_info(self, vi: VideoInfo):
        self.info = vi
        self.lbl_thumb.clear()
        self.lbl_title.setText(vi.title)
        self.lbl_meta.setText(f"{vi.channel}  -  {fmt_duration(vi.duration)}" if vi.duration else vi.channel)
        if vi.thumbnail_bytes:
            pm = QPixmap()
            if pm.loadFromData(vi.thumbnail_bytes):
                self.lbl_thumb.setPixmap(pm.scaled(self.lbl_thumb.size(),
                                                   Qt.AspectRatioMode.KeepAspectRatio,
                                                   Qt.TransformationMode.SmoothTransformation))
        if vi.segments:
            kind = {"manual": "manual", "auto": "auto-generated", "file": "from file"}.get(
                vi.transcript_kind, "captions")
            self.lbl_tr.setText(f"English transcript found ({kind}, {len(vi.segments)} lines).")
            self.lbl_tr.setStyleSheet("color:#1a7f37;")
            self.chk_tr.setEnabled(True)
            self.chk_tr.setChecked(True)
        else:
            self.lbl_tr.setText("No English transcript found - screenshots only (or attach a .srt/.vtt with 'Load transcript file...').")
            self.lbl_tr.setStyleSheet("color:#b35900;")
        self.playlist = None
        if self._list_source is not None and self._list_source != vi.source_url:
            self.list.clear()  # screenshots belong to a different video - don't mix them up
            self._list_source = None
            self.update_detail()
        if self._out_auto or not self.ed_out.text().strip():
            self.ed_out.setText(self._default_out())
        pending, self._pending_capture = self._pending_capture, False
        self._end_job()
        self.say("Video info loaded.")
        if pending:
            self.on_capture()

    # ---- Step 1: capture -------------------------------------------------- #
    def on_capture(self):
        raw = self.ed_url.text().strip()
        if not raw:
            QMessageBox.information(self, "YouTube link", "Please paste a YouTube link first.")
            return
        url = normalize_url(raw)
        if not self.info or self.info.source_url != url:
            self._pending_capture = True
            self.on_fetch()
            return
        try:
            start, end = parse_time(self.ed_start.text()), parse_time(self.ed_end.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Time range", str(exc))
            return
        if self.list.count() and QMessageBox.question(
                self, "Replace screenshots?",
                "This will discard the screenshots currently listed. Continue?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.list.clear()
        self._list_source = url
        self._update_count()
        w = CaptureWorker(url, self.info.duration, self.cb_quality.currentData(),
                          self.sp_interval.value(), self.sp_thresh.value(), start, end,
                          self.sp_max.value(), self.chk_blank.isChecked(),
                          self._net_cfg(), self.chk_update.isChecked(),
                          local=self.info.is_local, prefer=self.info.ctx)
        w.shot_ready.connect(self._add_shot)
        w.done.connect(self._on_capture_done)
        self._start(w)

    def _add_shot(self, t: float, jpeg: bytes, w: int, h: int):
        shot = Shot(t, jpeg, w, h)
        pm = QPixmap()
        pm.loadFromData(jpeg)
        item = QListWidgetItem(QIcon(pm.scaled(176, 99, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation)),
                               f"#{self.list.count() + 1}  -  {fmt_duration(t)}")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setData(Qt.ItemDataRole.UserRole, shot)
        self.list.blockSignals(True)
        item.setCheckState(Qt.CheckState.Checked)
        self.list.addItem(item)
        self.list.blockSignals(False)
        self._update_count()

    def _on_capture_done(self, kept: int):
        self._end_job()
        self.say(f"Done - {kept} unique screenshots captured. Review them, then click 'Export PDF'.")
        if self.list.count():
            self.list.setCurrentRow(0)

    # ---- Review helpers --------------------------------------------------- #
    def _items(self):
        return [self.list.item(i) for i in range(self.list.count())]

    def _checked_shots(self) -> List[Shot]:
        return [it.data(Qt.ItemDataRole.UserRole) for it in self._items()
                if it.checkState() == Qt.CheckState.Checked]

    def _update_count(self):
        total, ck = self.list.count(), len(self._checked_shots())
        self.lbl_count.setText(f"Screenshots: {ck} selected / {total} captured "
                               f"(untick to drop; double-click toggles)")

    def _toggle_item(self, item: QListWidgetItem):
        item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked
                           else Qt.CheckState.Checked)

    def _set_all(self, state):
        self.list.blockSignals(True)
        for it in self._items():
            it.setCheckState(state)
        self.list.blockSignals(False)
        self.update_detail()

    def on_delete_unchecked(self):
        for i in reversed(range(self.list.count())):
            if self.list.item(i).checkState() != Qt.CheckState.Checked:
                self.list.takeItem(i)
        self.update_detail()

    def update_detail(self, *_):
        self._update_count()
        item = self.list.currentItem()
        if not item:
            self.preview.clear()
            self.preview.setText("Select a screenshot to preview it here")
            self.detail.clear()
            return
        shot: Shot = item.data(Qt.ItemDataRole.UserRole)
        pm = QPixmap()
        pm.loadFromData(shot.jpeg)
        self.preview.setPixmap(pm.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
        if not (self.info and self.info.segments):
            self.detail.setPlainText("(No transcript available for this video.)")
            return
        checked = self._checked_shots()
        idx = next((i for i, s in enumerate(checked) if s is shot), None)
        if idx is None:
            self.detail.setPlainText("(This screenshot is unchecked - its transcript is merged "
                                     "into the previous checked screenshot.)")
            return
        groups = assign_transcript([s.time for s in checked], self.info.segments)
        raw = merge_segments(groups[idx])
        if not raw:
            self.detail.setPlainText("(No speech for this screenshot.)")
        elif self.chk_refine.isChecked() or self.chk_ai.isChecked():
            self.detail.setPlainText("\n\n".join(refine_local(raw)))
        else:
            self.detail.setPlainText(raw)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.list.currentItem():
            self.update_detail()

    # ---- Step 2: export --------------------------------------------------- #
    def on_browse(self):
        start = self.ed_out.text() or os.path.expanduser("~")
        if self.playlist and self.cb_plmode.currentData() == "separate":
            path = QFileDialog.getExistingDirectory(self, "Choose output folder", start)
        else:
            path, _ = QFileDialog.getSaveFileName(self, "Save PDF", start, "PDF files (*.pdf)")
        if path:
            self.ed_out.setText(path)
            self._out_auto = False

    def on_export(self):
        shots = self._checked_shots()
        if not shots:
            QMessageBox.information(self, "Nothing to export",
                                    "Capture screenshots first (and keep at least one ticked).")
            return
        if self.info and self._list_source and self._list_source != self.info.source_url:
            QMessageBox.warning(self, "Different video",
                                "These screenshots belong to a different video.\n"
                                "Click 'Capture screenshots' for the current video first.")
            return
        out = self.ed_out.text().strip()
        if not out:
            self.on_browse()
            out = self.ed_out.text().strip()
            if not out:
                return
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
            self.ed_out.setText(out)
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

        include = bool(self.info and self.info.segments) and self.chk_tr.isChecked()
        ai = None
        if include and self.chk_ai.isChecked():
            key, model = self.ed_key.text().strip(), self.ed_model.text().strip()
            if not key or not model:
                QMessageBox.warning(self, "Claude polish", "Enter an API key and model name, "
                                    "or untick 'Polish with Claude'.")
                return
            ai = {"key": key, "model": model}

        meta = {"title": self.info.title if self.info else "YouTube screenshots",
                "channel": self.info.channel if self.info else "",
                "url": "" if (not self.info or self.info.is_local) else self.info.source_url}
        w = ExportWorker(shots, self.info.segments if self.info else [], include,
                         self.chk_refine.isChecked(), ai, meta,
                         PAGE_SIZES[self.cb_page.currentText()],
                         self.chk_newpage.isChecked(), out)
        w.done.connect(self._on_export_done)
        self._start(w)

    def _on_export_done(self, path: str):
        self._end_job()
        self._out_auto = True  # next video gets its own file name
        self._out_dir = os.path.dirname(os.path.abspath(path))
        self.say(f"PDF saved: {path}")
        box = QMessageBox(self)
        box.setWindowTitle("PDF created")
        box.setText(f"Saved to:\n{path}")
        b_open = box.addButton("Open PDF", QMessageBox.ButtonRole.AcceptRole)
        b_dir = box.addButton("Show folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is b_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        elif box.clickedButton() is b_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(os.path.abspath(path))))

    def closeEvent(self, ev):
        for t in list(self._threads):
            if t.isRunning():
                t.cancel()
                t.wait(5000)
        super().closeEvent(ev)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    sys.excepthook = win.on_uncaught
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()