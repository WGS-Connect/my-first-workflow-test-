from __future__ import annotations
import json, logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("audiobook")

def now():
    return datetime.now(timezone.utc).isoformat()

def default(book_id, topic):
    return {
        "book_id": book_id, "topic": topic, "status": "NOT_STARTED",
        "current_stage": "TOPIC", "last_completed_chapter": -1,
        "completed_chunks": [], "total_chunks": 0,
        "audio_completed": False, "video_completed": False,
        "thumbnail_completed": False, "metadata_completed": False,
        "youtube_uploaded": False, "youtube_video_id": None,
        "youtube_uploaded_at": None, "youtube_upload_intent": None,
        "tts_provider": None, "chapter_count": 0,
        "updated_at": now(), "error": None, "error_class": None
    }

def save(local: Path, drive, st):
    st["updated_at"] = now()
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    drive.put_file(local, f"WORK/{st['book_id']}/state.json")

def load(local: Path, drive, book_id):
    try:
        drive.download_file_by_remote(f"WORK/{book_id}/state.json", local)
        return json.loads(local.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("Could not load remote state for %s: %s", book_id, exc)
        return None

def valid_file(p, min_bytes=32):
    return p.exists() and p.is_file() and p.stat().st_size >= min_bytes

def remote_restore(drive, remote, local, validator=None):
    if validator and validator(local):
        return True
    if local.exists() and not validator:
        return True
    try:
        drive.download_file_by_remote(remote, local)
        return validator(local) if validator else True
    except FileNotFoundError:
        return False

def remote_checkpoint(drive, local, remote):
    if not Path(local).exists():
        raise FileNotFoundError(local)
    drive.put_file(Path(local), remote)
