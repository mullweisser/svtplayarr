import logging
import os
import re
import subprocess
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".m4v", ".ts", ".avi", ".mov", ".webm"}


def load_download_config():
    path = Path(os.environ.get("CONFIG_DIR", "/config")) / "config.yml"
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except OSError as exc:
        logger.error("Could not read download config: %s", exc)
        return {}


def build_svtplay_dl_command(url, output_dir, config, filename=None):
    options = (config or {}).get("svtplay_dl") or {}
    command = ["svtplay-dl"]
    if options.get("quality"):
        command.extend(["-q", str(options["quality"])])
    if options.get("subtitle"):
        command.append("--subtitle")
    if options.get("force_subtitle"):
        command.append("--force-subtitle")
    command.extend(["-o", output_dir])
    if filename:
        command.extend(["--filename", filename])
    if options.get("remux"):
        command.append("--remux")
    if options.get("merge_subtitle"):
        command.append("--merge-subtitle")
    if options.get("thumbnail"):
        command.append("--thumbnail")
    command.append(url)
    return command


def download_job(job, download_root, config=None):
    category = job.get("category") or "tv"
    if category in ("*", ""):
        folder = download_root
    else:
        safe_category = re.sub(r"[^\w.-]+", "_", category)
        folder = os.path.join(download_root, safe_category)
    safe_title = re.sub(r"[\\/]+", "-", job["title"])[:180]
    destination = os.path.join(folder, safe_title)
    Path(destination).mkdir(parents=True, exist_ok=True)
    command = build_svtplay_dl_command(
        job["url"],
        destination,
        config if config is not None else load_download_config(),
        filename=safe_title,
    )
    logger.info("Starting grab: %s", " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "svtplay-dl failed").strip()
        raise RuntimeError(detail[-500:])
    if not _has_video(destination):
        raise RuntimeError("svtplay-dl finished without a video file")
    return destination


def _has_video(directory):
    for path in Path(directory).rglob("*"):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            return True
    return False
