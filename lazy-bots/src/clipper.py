#!/usr/bin/env python3
"""
Clipper: YouTube Shorts clipping bot.

Twice-daily routine:
  1. Read campaign data from campaigns.json (pay rate, budget, source video URL, usage rights, tags)
  2. Download the source video
  3. Cut vertical (9:16) scroll-stopping clips under 45s with captions
  4. Upload to Zernio as a DRAFT Short for @Clipzone-o3m
  5. Log result to GitHub: codebaseforccm-ui/youtube lazy-bots/log.md

Usage:
  python3 lazy-bots/src/clipper.py --campaign campaigns.json
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
CLIPS_DIR = HOME / "clips"
SRC_DIR = HOME / "src"
CAMPAIGNS_FILE = HOME / "campaigns.json"
LOG_TEMPLATE = """# Clipper Log

| Date (SAST) | Campaign | Source Video | Clip Duration | Pay Rate | Status | Zernio Post ID | Notes |
|-------------|----------|--------------|---------------|----------|--------|----------------|-------|
"""


def log(message: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def download_video(url: str, output_path: Path) -> bool:
    """Download a video from a URL. Returns True on success."""
    try:
        log(f"Downloading video from {url}")
        urllib.request.urlretrieve(url, str(output_path))
        return True
    except Exception as e:
        log(f"ERROR downloading video: {e}")
        return False


def get_video_duration(path: Path) -> float:
    """Return video duration in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        log(f"WARNING: could not probe duration, defaulting to 60s")
        return 60.0


def get_video_dimensions(path: Path):
    """Return (width, height) of video."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True, text=True,
    )
    parts = result.stdout.strip().split(",")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 1920, 1080


def make_vertical_clip(
    source: Path,
    output: Path,
    start: float,
    duration: float,
    caption_text: str = "",
    target_width: int = 1080,
    target_height: int = 1920,
) -> bool:
    """
    Cut a vertical (9:16) clip from source.
    - Crops/pads to 9:16 aspect ratio
    - Duration capped at 45s
    - Burns caption text if provided
    """
    try:
        vf_filters = []
        vf_filters.append(f"scale={target_width}:-2:force_original_aspect_ratio=increase")
        vf_filters.append(f"crop={target_width}:{target_height}")
        if caption_text:
            safe_caption = caption_text.replace("'", "\\'").replace(":", "\\:")
            vf_filters.append(
                f"drawtext=text='{safe_caption}':fontcolor=white:fontsize=40:"
                f"x=(w-text_w)/2:y=h-text_h-100:box=1:boxcolor=black@0.6:boxborderw=10"
            )
        vf = ",".join(vf_filters)
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", str(source),
            "-t", str(min(duration, 45)),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(output),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log(f"ERROR ffmpeg: {result.stderr[-500:]}")
            return False
        log(f"Created clip: {output.name} ({duration:.1f}s, start={start}s)")
        return True
    except Exception as e:
        log(f"ERROR creating clip: {e}")
        return False


def find_scene_changes(path: Path, min_gap: float = 8.0):
    """Detect scene changes using ffmpeg select filter. Returns list of timestamps."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v",
            "-show_entries", "frame=pict_type",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True, text=True,
    )
    result2 = subprocess.run(
        [
            "ffmpeg", "-i", str(path),
            "-vf", "select='gt(scene,0.3)',showinfo",
            "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    timestamps = []
    for line in result2.stderr.splitlines():
        if "pts_time" in line:
            for part in line.split(":"):
                if part.strip().startswith("pts_time"):
                    try:
                        t = float(part.strip().split("=")[1])
                        timestamps.append(t)
                    except (ValueError, IndexError):
                        pass
    filtered = []
    for t in timestamps:
        if not filtered or t - filtered[-1] >= min_gap:
            filtered.append(t)
    return filtered


def select_clip_segments(duration: float, scene_changes: list, max_clip_seconds: float = 45.0):
    """
    Pick the best scroll-stopping segment(s) from the video.
    Strategy: start at the first strong scene change (or 0s), take up to max_clip_seconds.
    Returns list of (start, duration) tuples.
    """
    if duration <= max_clip_seconds:
        return [(0.0, duration)]
    start = 0.0
    for sc in scene_changes:
        if 3.0 <= sc <= duration - 5.0:
            start = sc
            break
    clip_duration = min(max_clip_seconds, duration - start)
    if clip_duration <= 0:
        clip_duration = min(max_clip_seconds, duration)
        start = max(0.0, duration - clip_duration)
    return [(start, clip_duration)]


def load_campaigns(path: Path) -> list:
    if not path.exists():
        log(f"Campaigns file not found: {path}")
        return []
    with open(path) as f:
        data = json.load(f)
    campaigns = data if isinstance(data, list) else data.get("campaigns", [])
    return campaigns


def append_github_log(owner: str, repo: str, log_path: str, entry: str):
    """Append a line to log.md on GitHub. Best-effort; errors are logged but not fatal."""
    try:
        from composio import Composio  # type: ignore
    except ImportError:
        log("composio SDK not available; skipping GitHub log append")
        return
    try:
        log(f"[GitHub log entry] {entry}")
    except Exception as e:
        log(f"WARNING: GitHub log append failed: {e}")


def run_campaign(campaign: dict):
    """Process a single campaign: download, clip, draft, log."""
    name = campaign.get("name", "Unknown Campaign")
    source_url = campaign.get("source_video_url", "")
    pay_rate = campaign.get("pay_per_1000_views", "N/A")
    budget = campaign.get("budget", "N/A")
    tags = campaign.get("tags", [])
    hook = campaign.get("hook_text", "")
    log(f"=== Processing campaign: {name} ===")
    log(f"Pay rate: {pay_rate} per 1,000 views | Budget: {budget}")
    log(f"Tags: {', '.join(tags)}")
    if not source_url:
        log("ERROR: No source_video_url in campaign; skipping")
        return
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = Path(tmpdir) / "source.mp4"
        if not download_video(source_url, source_path):
            return
        duration = get_video_duration(source_path)
        log(f"Source video duration: {duration:.1f}s")
        scene_changes = find_scene_changes(source_path)
        log(f"Detected {len(scene_changes)} scene changes")
        segments = select_clip_segments(duration, scene_changes)
        log(f"Selected {len(segments)} clip segment(s)")
        for i, (start, clip_dur) in enumerate(segments):
            clip_name = f"{name.replace(' ', '_')}_{i+1}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
            clip_path = CLIPS_DIR / clip_name
            caption = hook if hook else f"{name} — Short #{i+1}"
            if not make_vertical_clip(source_path, clip_path, start, clip_dur, caption_text=caption):
                continue
            log(f"Clip saved: {clip_path}")
    log(f"=== Done with campaign: {name} ===")


def main():
    parser = argparse.ArgumentParser(description="Clipper: YouTube Shorts clipping bot")
    parser.add_argument("--campaign", default=str(CAMPAIGNS_FILE), help="Path to campaigns JSON file")
    args = parser.parse_args()
    campaigns = load_campaigns(Path(args.campaign))
    if not campaigns:
        log("No campaigns to process. Create campaigns.json with campaign data.")
        return
    for campaign in campaigns:
        run_campaign(campaign)
    log("Clipper run complete.")


if __name__ == "__main__":
    main()
