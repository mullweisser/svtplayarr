import logging
import os
import threading

from flask import Blueprint, current_app, jsonify, request

from auth import api_key_matches
from downloader import download_job
from newznab import token_from_nzb

logger = logging.getLogger(__name__)

bp = Blueprint("sabnzbd", __name__)
_worker_started = False
_worker_lock = threading.Lock()


def _authorized():
    expected = current_app.config["INDEXER_API_KEY"]
    given = request.values.get("apikey") or request.headers.get("X-Api-Key")
    return api_key_matches(given, expected)


def _download_root():
    return current_app.config.get("DOWNLOAD_ROOT") or os.environ.get("DOWNLOAD_ROOT", "/downloads")


def _positive_int(raw, default):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def process_one(store, downloader):
    job = store.claim_next()
    if job is None:
        return None
    try:
        storage = downloader(job)
    except Exception as exc:
        logger.error("Grab %s failed: %s", job["nzo_id"], exc)
        store.mark_failed(job["nzo_id"], str(exc))
        return job["nzo_id"]
    store.mark_completed(job["nzo_id"], storage)
    return job["nzo_id"]


def _worker_loop(app, stop_event):
    while not stop_event.is_set():
        with app.app_context():
            store = app.config["STORE"]
            root = app.config.get("DOWNLOAD_ROOT") or "/downloads"

            def downloader(job):
                return download_job(job, root)

            worked = process_one(store, downloader)
        if not worked:
            stop_event.wait(2)


def start_worker(app):
    global _worker_started
    if os.environ.get("SVTPLAYARR_DISABLE_WORKER") == "1":
        return None
    with _worker_lock:
        if _worker_started:
            return None
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_worker_loop,
            args=(app, stop_event),
            name="svtplayarr-grab",
            daemon=True,
        )
        thread.start()
        _worker_started = True
        return thread


def _queue_payload(jobs, limit):
    slots = []
    for index, job in enumerate(jobs[:limit]):
        total_mb = max((job["bytes"] or 0) / (1024 * 1024), 1)
        remaining = total_mb * (100 - int(job["percentage"])) / 100
        slots.append({
            "index": index,
            "status": job["status"],
            "nzo_id": job["nzo_id"],
            "filename": job["title"],
            "cat": job["category"],
            "mb": round(total_mb, 2),
            "mbleft": round(remaining, 2),
            "percentage": int(job["percentage"]),
            "timeleft": "0:00:00" if job["status"] != "Downloading" else "0:10:00",
        })
    return {
        "queue": {
            "paused": False,
            "slots": slots,
            "speed": "0",
            "kbpersec": 0,
            "mb": round(sum(slot["mb"] for slot in slots), 2),
            "mbleft": round(sum(slot["mbleft"] for slot in slots), 2),
            "noofslots": len(slots),
            "status": "Downloading" if any(slot["status"] == "Downloading" for slot in slots) else "Idle",
            "timeleft": "0:00:00",
        }
    }


def _history_payload(jobs, limit):
    slots = []
    for job in jobs[:limit]:
        slots.append({
            "nzo_id": job["nzo_id"],
            "name": job["title"],
            "nzb_name": f'{job["title"]}.nzb',
            "category": job["category"],
            "status": job["status"],
            "bytes": int(job["bytes"] or 0),
            "storage": job["storage"] or "",
            "fail_message": job["fail_message"] or "",
            "download_time": 0,
        })
    return {"history": {"paused": False, "slots": slots, "noofslots": len(slots)}}


def _config_payload():
    # Sonarr treats history_retention_option "all" as keep forever.
    # complete_dir is absolute so Sonarr joins it with the relative category dir.
    root = _download_root()
    return {
        "config": {
            "misc": {
                "complete_dir": root,
                "history_retention": "",
                "history_retention_option": "all",
                "history_retention_number": 1,
            },
            "categories": [
                {"name": "*", "dir": "", "priority": 0},
                {"name": "tv", "dir": "tv", "priority": 0},
                {"name": "movies", "dir": "movies", "priority": 0},
            ],
        }
    }


@bp.route("/api", methods=["GET", "POST"])
def api():
    if not _authorized():
        return jsonify({"status": False, "error": "API Key Incorrect"}), 401
    mode = (request.values.get("mode") or "").lower()
    store = current_app.config["STORE"]
    action = request.values.get("name")

    if mode == "version":
        return jsonify({"version": "4.3.0"})
    if mode == "fullstatus":
        # Sonarr reads the completed directory from this key, not complete_dir.
        return jsonify({"status": {"completedir": _download_root(), "version": "4.3.0"}})
    if mode == "get_config":
        return jsonify(_config_payload())
    if mode == "addfile":
        upload = request.files.get("name") or request.files.get("nzbfile")
        payload = upload.read() if upload is not None else request.get_data()
        token = token_from_nzb(payload or b"")
        release = store.get_release(token) if token else None
        if release is None:
            return jsonify({"status": False, "error": "Unknown NZB"})
        category = request.values.get("cat") or release["category"]
        nzo_id = store.add_job(release, category)
        return jsonify({"status": True, "nzo_ids": [nzo_id]})
    if mode == "queue" and action == "delete":
        store.delete(request.values.get("value", ""))
        return jsonify({"status": True})
    if mode == "history" and action == "delete":
        store.delete(request.values.get("value", ""))
        return jsonify({"status": True})
    if mode == "retry":
        nzo_id = store.retry(request.values.get("value", ""))
        if not nzo_id:
            return jsonify({"status": False, "error": "Unknown job"})
        return jsonify({"status": True, "id": nzo_id})
    if mode == "queue":
        limit = _positive_int(request.values.get("limit"), 100)
        category = request.values.get("category") or None
        return jsonify(_queue_payload(store.queue(category), limit))
    if mode == "history":
        limit = _positive_int(request.values.get("limit"), 100)
        category = request.values.get("category") or None
        return jsonify(_history_payload(store.history(category), limit))
    return jsonify({"status": False, "error": f"Unsupported mode: {mode}"}), 400
