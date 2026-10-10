# YouTube → Screenshot PDF Studio

A desktop app (Python + PyQt6) that turns a YouTube video, or a whole playlist, into a clean PDF of **de-duplicated screenshots**, each followed by a **readable transcript paragraph** (no timestamps).

Useful for lecture notes, tutorials, slide-based talks and any video you want to study offline.

---
[Download The Windows Executable File](https://drive.google.com/file/d/1TzSkkqKUNbieFsOz-kFNhAqfUYZMBhR-/view?usp=sharing)
## Features

- **Paste any YouTube link**: normal, `youtu.be`, Shorts, mobile, links with `&t=` or `&list=` extras, or a bare video ID.
- **Smart screenshots**: samples the video at an interval you choose, skips black/blank frames, and drops frames that look like ones already kept. A slide that stays on screen is captured once.
- **Review before exporting**: thumbnail grid with a large preview. Untick any screenshot you don't want.
- **Transcripts under every screenshot**:
  - Uses YouTube's English captions (manual first, auto-generated as fallback).
  - Transcript text always follows its screenshot. If a screenshot is removed, its speech merges into the previous one.
  - Optional offline refinement turns raw captions into readable paragraphs.
  - Optional AI polish with Claude (bring your own API key).
- **Entire playlists**: pick the videos you want, then get one PDF per video or one combined PDF.
- **Reliability layer**: automatic fallbacks, `yt-dlp` auto-update, browser-cookie support, and a local-file mode that works even when YouTube blocks downloads.
- **PDF options**: A4, US Letter or A4 landscape; optionally one screenshot per page; clickable source link and page numbers.

---

## ⚠️ If you're using the compiled `.exe` instead of the Python script
 
The standalone executable is built with PyInstaller, which bundles a Python runtime into a single file. Because the EXE is unsigned and self-extracts at startup, antivirus tools sometimes flag it even though it's clean — this is a very common false positive for small/independent tools built this way, not a sign the file is unsafe.
 
**What you might see:**
- Windows Defender SmartScreen: *"Windows protected your PC"* / *"Unrecognized app"*
- Windows Defender real-time protection: quarantines or deletes the EXE on download or first run, often under a generic heuristic name like `Trojan:Win32/Wacatac`, `Trojan:Script/Wacatac`, `Program:Win32/Wacapew`, or similar
- Other antivirus products (Avast, AVG, Norton, McAfee, etc.): generic labels like `Gen:Variant.*`, `PUA.*`, `Heur.*`, or `Suspicious.*`
- Browser download warning: *"This file isn't commonly downloaded and could be dangerous"*
**Why this happens:** the EXE has no publisher reputation (it's new and unsigned), and PyInstaller's packaging pattern structurally resembles how some malware droppers behave, even though the actual code is just this app. You can verify this yourself by reading `yt_screenshot_pdf.py` — the EXE is built directly from it — or by checking the scan results on [VirusTotal](https://www.virustotal.com) and seeing that only a handful of heuristic/generic engines flag it, with no agreement on what it supposedly is.
 
**If Defender or your antivirus blocks it and you trust the source you got it from:**
 
1. Open **Windows Security** → **Virus & threat protection**.
2. Under **Virus & threat protection settings**, click **Manage settings**.
3. If it was already quarantined: go to **Protection history**, find the entry, and click **Actions → Allow** (or **Restore**).
4. To stop it being flagged again, add an exclusion: **Manage settings → Add or remove exclusions → Add an exclusion → File**, and select the EXE (or its folder).
5. Re-download/re-run the EXE.
Only do this for a copy of the EXE you built yourself or downloaded from a source you trust — an exclusion also stops Defender scanning that file in the future, so don't use it as a blanket workaround for files you're unsure about.
 
**Prefer not to touch antivirus settings at all?** Run the Python script directly instead (see *Installation* and *Running* below) — it's the exact same code with nothing compiled, so there's nothing for a scanner to flag.
 
---

## Requirements

- Python **3.9+**
- Packages:

```
PyQt6
yt-dlp
opencv-python
numpy
reportlab
youtube-transcript-api   # optional, extra transcript fallback
```

`ffmpeg` is **not** required (only a video-only stream is downloaded).

## Installation

**Windows**

```powershell
py -m pip install PyQt6 yt-dlp opencv-python numpy reportlab youtube-transcript-api
```

**macOS / Linux**

```bash
python3 -m pip install PyQt6 yt-dlp opencv-python numpy reportlab youtube-transcript-api
```

## Running

```powershell
py yt_screenshot_pdf.py          # Windows
python3 yt_screenshot_pdf.py     # macOS / Linux
```

> **VS Code + Code Runner on Windows:** the default Code Runner command uses `/usr/bin/env python3`, which does not exist on Windows. Run the file from the terminal as shown above, use the **Run Python File** button from Microsoft's Python extension, or set `"code-runner.executorMap": { "python": "py -u" }` in your VS Code settings.

---

## Quick start: single video

1. Paste a YouTube link and click **Fetch info**. The title, thumbnail and transcript availability appear.
2. Adjust the settings if you like (the defaults work well for slide-style videos).
3. Click **1. Capture screenshots**. The video is downloaded and scanned, and screenshots appear in the grid.
4. Review them: untick the ones you don't want, click one to preview it with its transcript.
5. Choose the transcript options and output file, then click **2. Export PDF**.

You can also skip Fetch info. Clicking **Capture screenshots** fetches the info first automatically.

## Playlists

1. Paste a playlist link (`youtube.com/playlist?list=...`). **Entire playlist** is ticked for you. For a `watch?v=...&list=...` link, tick it yourself.
2. Click **Fetch info**. The **Playlist** tab lists every video, all ticked. Untick any you don't want.
3. Under **PDF → Playlist output**, choose:
   - **One PDF per video**: a folder of files named `01 - Title.pdf`, `02 - Title.pdf`, and so on.
   - **One combined PDF**: one section per video, each starting on a new page.
4. Click **Process whole playlist → PDF(s)**.

Notes:

- Videos that fail (private, deleted, region-blocked) are skipped and listed in a summary at the end. The rest continue.
- A video without an English transcript gets screenshots only.
- The start/end time fields are disabled in playlist mode.
- A combined PDF keeps every screenshot in memory. For very large playlists, use one PDF per video.

---

## Settings reference

### Screenshot capture

| Setting | What it does | Default |
|---|---|---|
| Sample every | How often the video is checked for a new screenshot. Smaller values catch quick changes but take longer. | 5 s |
| Min. change for new shot | A frame is kept only if more than this % of its pixels differ from the recently kept screenshots. Raise it for fewer screenshots, lower it if slides get skipped. | 2 % |
| Video quality | Resolution of the downloaded video (360p–1080p). 720p is a good balance. | 720p |
| Time range | Optional start/end (`mm:ss` or `hh:mm:ss`). Single videos only. | whole video |
| Max screenshots | Stops after this many screenshots. | unlimited |
| Skip black / blank frames | Ignores fades, black intros and empty frames. | on |

### Transcript

| Setting | What it does |
|---|---|
| Include transcript under each screenshot | Adds the matching transcript text below every screenshot. Disabled when no English transcript exists. |
| Refine into readable paragraphs (offline) | Cleans the raw captions (details below). |
| Polish with Claude (optional) | Sends each transcript chunk to the Anthropic API for a higher-quality rewrite. Needs an API key and model name (default `claude-sonnet-5-5`). The key is read from the `ANTHROPIC_API_KEY` environment variable if set and is **never saved**. If the API fails, the app falls back to the offline refinement. |

**What offline refinement does:** merges the overlapping lines of auto-captions, removes tags like `[Music]` and `(applause)`, strips filler words ("um", "uh") and stuttered repeats ("the the"), adds sentence breaks and capitalisation, and groups sentences into short paragraphs.

### Connection & reliability

| Setting | What it does |
|---|---|
| Browser cookies | Uses your logged-in YouTube session (Firefox, Chrome, Edge, Brave, Opera, Vivaldi). **Auto** tries no cookies first, then installed browsers. Helps with bot checks and age-restricted videos. |
| cookies.txt | Use an exported cookies file instead of a browser (overrides the browser setting). |
| Auto-update yt-dlp if everything fails | Runs `pip install -U yt-dlp` once and retries. |
| Update yt-dlp now | Updates `yt-dlp` on demand. |

### PDF

| Setting | What it does |
|---|---|
| Page size | A4, US Letter or A4 landscape. |
| Start every screenshot on a new page | One screenshot per page instead of flowing several per page. |
| Save as | Output file (single video / combined PDF) or folder (one PDF per video). |
| Playlist output | Playlist mode only: per-video PDFs or one combined PDF. |

---

## Using a local video (always works)

If YouTube blocks the download, or you simply already have the file:

- Click **Open local video...** (or paste/drag a file path into the link box).
- A `.srt` or `.vtt` file with the same name next to the video is picked up automatically.
- Or click **Load transcript file...** to attach a `.srt`, `.vtt` or `.json3` transcript to any loaded video.

Everything else (duplicate removal, refinement, PDF export) works the same way. Screen recordings and videos from other downloaders work fine.

---

<img width="1919" height="1008" alt="image" src="https://github.com/user-attachments/assets/dc458f69-e9b4-455c-a8db-200f39e58079" />
<img width="1919" height="1005" alt="image" src="https://github.com/user-attachments/assets/9a10c6f3-f025-4bdb-81da-81ae874641de" />


## How it works

1. **Info**: `yt-dlp` reads the video metadata and caption tracks.
2. **Download**: a video-only stream is saved to a temporary folder (deleted afterwards). Several YouTube access methods are tried in turn, and `yt-dlp` is updated automatically if all fail.
3. **Sampling**: OpenCV seeks to each sample time, grabs a frame, shrinks it to 320×180 greyscale and compares it with the last six kept frames. Frames that are nearly black/uniform or too similar to a recent one are dropped.
4. **Transcript matching**: each caption line is attached to the last kept screenshot taken before it starts. Overlapping auto-caption lines are merged.
5. **PDF**: ReportLab places each screenshot with its transcript paragraphs below it. If a Unicode system font is found (Segoe UI, Arial, DejaVu Sans), non-Latin titles render correctly. Otherwise, characters outside Latin-1 are dropped.

---

## Troubleshooting

| Problem | Try this |
|---|---|
| `The system cannot find the path specified` in VS Code | Code Runner is using a Linux path. Run from the terminal with `py yt_screenshot_pdf.py`. |
| "Sign in to confirm you're not a bot" | Log in to YouTube in Firefox and set **Browser cookies** to Firefox or Auto. Changing network or turning off a VPN also helps. |
| Chrome/Edge cookies can't be read | Close the browser completely, use Firefox, or export a `cookies.txt` file. |
| Download or format errors | Click **Update yt-dlp now**, then retry. |
| Rate-limited (HTTP 429) | Wait 10–30 minutes or switch network. |
| Age-restricted / private / members-only video | Needs cookies from an account with access. Otherwise, use a local file. |
| Live streams / premieres | Not supported until they have ended. |
| "Permission denied" when exporting | The PDF is open in a viewer. Close it or choose another file name. |
| No transcript found | The video has no English captions. Screenshots still work, or load a `.srt`/`.vtt` file. |
| Too many or too few screenshots | Adjust **Min. change for new shot** and **Sample every**. |
| Anything else | Check the log box at the bottom-left. It lists each method that was tried and why it failed. |

**Always-works fallback:** get the video by any means (another downloader or a screen recording), then use **Open local video...**.

---

## Privacy

- Everything runs locally. The only network requests go to YouTube (via `yt-dlp`), PyPI (only if you update `yt-dlp`) and, if you enable Claude polish, the Anthropic API.
- With Claude polish on, the transcript text of your video is sent to Anthropic. Your API key is never written to disk by this app.
- Temporary video files are deleted after each run.

## Responsible use

Only download and copy videos you have the right to. Respect YouTube's Terms of Service and the creators' copyright. This tool is intended for personal study and note-taking.

## Known limitations

- Transcripts are English only.
- Auto-generated captions can contain recognition errors. Offline refinement fixes formatting, not wrong words (Claude polish can help with some of these).
- YouTube changes often. If something breaks, update `yt-dlp` first.
- Very long videos with a small sampling interval take a while, because every sample point requires a seek and decode.
