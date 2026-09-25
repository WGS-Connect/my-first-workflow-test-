```python
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


log = logging.getLogger("audiobook.state")


# ============================================================
# DEFAULT STATE
# ============================================================

def default(
    book_id: str,
    topic: str
) -> Dict[str, Any]:

    now = datetime.now(
        timezone.utc
    ).isoformat()

    return {
        "version": 2,

        "book_id": book_id,
        "topic": topic,

        "status": "PENDING",
        "current_stage": "NOT_STARTED",

        "created_at": now,
        "updated_at": now,
        "heartbeat_at": now,

        # Book generation
        "chapter_count": 12,
        "last_completed_chapter": 0,

        # TTS
        "tts_provider": None,
        "total_chunks": 0,
        "completed_chunks": [],

        # Media
        "audio_completed": False,
        "video_completed": False,
        "thumbnail_completed": False,

        # YouTube
        "metadata_completed": False,
        "youtube_upload_intent": None,
        "youtube_uploaded": False,
        "youtube_video_id": None,
        "youtube_uploaded_at": None,

        # Errors
        "error": None,
        "error_class": None,

        # Recovery
        "resume_count": 0
    }


# ============================================================
# ATOMIC LOCAL JSON WRITE
# ============================================================

def _atomic_write(
    path: Path,
    data: Dict[str, Any]
):

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    payload = json.dumps(
        data,
        indent=2,
        ensure_ascii=False
    )

    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent)
    )

    try:

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8"
        ) as handle:

            handle.write(
                payload
            )

            handle.flush()

            os.fsync(
                handle.fileno()
            )

        os.replace(
            temp_name,
            path
        )

    except Exception:

        try:
            os.unlink(
                temp_name
            )
        except OSError:
            pass

        raise


# ============================================================
# SAVE STATE
# ============================================================

def save(
    local_path: Path,
    drive,
    state: Dict[str, Any]
):

    now = datetime.now(
        timezone.utc
    ).isoformat()

    state[
        "updated_at"
    ] = now

    state[
        "heartbeat_at"
    ] = now

    # Keep resume count useful without
    # accidentally increasing it on every checkpoint.
    state.setdefault(
        "resume_count",
        0
    )

    _atomic_write(
        local_path,
        state
    )

    # The Drive copy is the durable checkpoint.
    remote = (
        f"WORK/{state['book_id']}/state.json"
    )

    try:

        drive.put_file(
            local_path,
            remote
        )

    except Exception as exc:

        log.error(
            "Failed to upload state checkpoint: %s",
            exc
        )

        raise


# ============================================================
# LOAD STATE
# ============================================================

def load(
    local_path: Path,
    drive,
    book_id: str
):

    # --------------------------------------------------------
    # LOCAL FIRST
    # --------------------------------------------------------

    if local_path.exists():

        try:

            with local_path.open(
                "r",
                encoding="utf-8"
            ) as handle:

                data = json.load(
                    handle
                )

            if isinstance(
                data,
                dict
            ):

                return _normalize(
                    data,
                    book_id
                )

        except Exception as exc:

            log.warning(
                "Local state is unreadable: %s",
                exc
            )

    # --------------------------------------------------------
    # DRIVE FALLBACK
    # --------------------------------------------------------

    remote = (
        f"WORK/{book_id}/state.json"
    )

    try:

        drive.download_file_by_remote(
            remote,
            local_path
        )

    except FileNotFoundError:

        return None

    except Exception as exc:

        log.warning(
            "Could not restore state from Drive: %s",
            exc
        )

        return None

    # --------------------------------------------------------
    # READ RESTORED STATE
    # --------------------------------------------------------

    try:

        with local_path.open(
            "r",
            encoding="utf-8"
        ) as handle:

            data = json.load(
                handle
            )

        if not isinstance(
            data,
            dict
        ):

            return None

        normalized = _normalize(
            data,
            book_id
        )

        # Restore normalized state locally.
        _atomic_write(
            local_path,
            normalized
        )

        return normalized

    except Exception as exc:

        log.warning(
            "Restored Drive state is invalid: %s",
            exc
        )

        return None


# ============================================================
# STATE NORMALIZATION
# ============================================================

def _normalize(
    state: Dict[str, Any],
    book_id: str
):

    state.setdefault(
        "version",
        2
    )

    state.setdefault(
        "book_id",
        book_id
    )

    state.setdefault(
        "topic",
        ""
    )

    state.setdefault(
        "status",
        "PENDING"
    )

    state.setdefault(
        "current_stage",
        "NOT_STARTED"
    )

    state.setdefault(
        "chapter_count",
        12
    )

    state.setdefault(
        "last_completed_chapter",
        0
    )

    state.setdefault(
        "tts_provider",
        None
    )

    state.setdefault(
        "total_chunks",
        0
    )

    state.setdefault(
        "completed_chunks",
        []
    )

    state.setdefault(
        "audio_completed",
        False
    )

    state.setdefault(
        "video_completed",
        False
    )

    state.setdefault(
        "thumbnail_completed",
        False
    )

    state.setdefault(
        "metadata_completed",
        False
    )

    state.setdefault(
        "youtube_upload_intent",
        None
    )

    state.setdefault(
        "youtube_uploaded",
        False
    )

    state.setdefault(
        "youtube_video_id",
        None
    )

    state.setdefault(
        "youtube_uploaded_at",
        None
    )

    state.setdefault(
        "error",
        None
    )

    state.setdefault(
        "error_class",
        None
    )

    state.setdefault(
        "resume_count",
        0
    )

    now = datetime.now(
        timezone.utc
    ).isoformat()

    state.setdefault(
        "created_at",
        now
    )

    state.setdefault(
        "updated_at",
        now
    )

    state.setdefault(
        "heartbeat_at",
        now
    )

    # --------------------------------------------------------
    # SAFELY NORMALIZE CHAPTER NUMBER
    # --------------------------------------------------------

    try:

        state[
            "last_completed_chapter"
        ] = int(
            state.get(
                "last_completed_chapter",
                0
            )
        )

    except (
        TypeError,
        ValueError
    ):

        state[
            "last_completed_chapter"
        ] = 0

    # Never allow an invalid chapter number.
    state[
        "last_completed_chapter"
    ] = max(
        0,
        min(
            12,
            state[
                "last_completed_chapter"
            ]
        )
    )

    # --------------------------------------------------------
    # NORMALIZE COMPLETED TTS CHUNKS
    # --------------------------------------------------------

    chunks = state.get(
        "completed_chunks",
        []
    )

    if not isinstance(
        chunks,
        list
    ):

        chunks = []

    clean_chunks = []

    for item in chunks:

        try:

            value = int(
                item
            )

            if value > 0:

                clean_chunks.append(
                    value
                )

        except (
            TypeError,
            ValueError
        ):

            continue

    state[
        "completed_chunks"
    ] = sorted(
        set(
            clean_chunks
        )
    )

    return state


# ============================================================
# VALIDATE A COMPLETED FILE
# ============================================================

def valid_file(
    path: Path,
    minimum_size: int = 1
) -> bool:

    try:

        return (
            path.exists()
            and path.is_file()
            and path.stat().st_size
            >= minimum_size
        )

    except OSError:

        return False


# ============================================================
# MARK RESUME
# ============================================================

def mark_resumed(
    state: Dict[str, Any]
):

    state[
        "resume_count"
    ] = int(
        state.get(
            "resume_count",
            0
        )
    ) + 1

    state[
        "status"
    ] = "RUNNING"

    state[
        "error"
    ] = None

    state[
        "error_class"
    ] = None

    state[
        "updated_at"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    return state


# ============================================================
# DETERMINE NEXT CHAPTER
# ============================================================

def next_chapter(
    state: Dict[str, Any]
) -> int:

    completed = int(
        state.get(
            "last_completed_chapter",
            0
        )
    )

    return min(
        completed + 1,
        12
    )


# ============================================================
# IS BOOK COMPLETE?
# ============================================================

def book_complete(
    state: Dict[str, Any]
) -> bool:

    return (
        state.get(
            "status"
        ) == "SUCCESS"
        or
        (
            int(
                state.get(
                    "last_completed_chapter",
                    0
                )
            ) >= 12
            and bool(
                state.get(
                    "youtube_uploaded"
                )
            )
        )
    )
```
