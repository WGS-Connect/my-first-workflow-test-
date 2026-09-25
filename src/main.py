from __future__ import annotations

import json
import logging
import random
import re
import shutil
import threading
import time

from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.errors import HttpError

from .config import ROOT, CONFIG, VOICE, secret_config
from .drive import Drive
from .state import default, save, load, valid_file
from .gemini import Gemini
from .tts import TTS
from .video import (
    assemble_audio,
    build_video,
    valid_media,
    duration,
    assemble_video_clips,
)
from .thumbnail import make
from .youtube import YouTube


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("audiobook")


# ============================================================
# ERROR CLASSIFICATION
# ============================================================

def classify_error(exc):

    status = getattr(exc, "status_code", None)

    if status is None and isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)

    text = str(exc).lower()

    if status == 401 or any(
        x in text
        for x in (
            "invalid api key",
            "authentication",
            "unauthorized"
        )
    ):
        return "AUTH_ERROR"

    if status == 400 or any(
        x in text
        for x in (
            "invalid request",
            "invalid argument"
        )
    ):
        return "INVALID_REQUEST"

    if status == 404 or "not found" in text:
        return "MISSING_FILE"

    if status in (429, 500, 502, 503, 504) or any(
        x in text
        for x in (
            "rate limit",
            "quota exceeded",
            "dailylimit",
            "temporarily unavailable",
            "timeout"
        )
    ):

        if "quota" in text or "dailylimit" in text:
            return "QUOTA_EXCEEDED"

        return "TEMPORARY"

    if "corrupt" in text or "invalid data" in text:
        return "CORRUPT_FILE"

    if isinstance(exc, (FileNotFoundError, ValueError)):
        return "PERMANENT"

    return "TEMPORARY"


# ============================================================
# RETRY SYSTEM
# ============================================================

class Retry:

    def __init__(self, n, lo, hi):

        self.n = int(n)
        self.lo = float(lo)
        self.hi = float(hi)

    def __call__(self, fn, provider, model=None):

        last = None

        for attempt in range(1, self.n + 1):

            try:

                log.info(
                    "PROVIDER %s MODEL %s ATTEMPT %s/%s",
                    provider,
                    model or "-",
                    attempt,
                    self.n
                )

                return fn()

            except Exception as exc:

                last = exc

                error_class = classify_error(exc)

                if error_class in {
                    "AUTH_ERROR",
                    "INVALID_REQUEST",
                    "MISSING_FILE",
                    "CORRUPT_FILE"
                }:
                    raise

                if attempt == self.n:
                    raise

                delay = random.uniform(
                    self.lo,
                    self.hi
                )

                log.warning(
                    "%s failed: %s; retrying in %.1fs",
                    provider,
                    exc,
                    delay
                )

                time.sleep(delay)

        raise last


# ============================================================
# HEARTBEAT
# ============================================================

class Heartbeat:

    def __init__(
        self,
        local_state,
        drive,
        state,
        interval=15
    ):

        self.local_state = local_state
        self.drive = drive
        self.state = state
        self.interval = interval

        self.stop_event = threading.Event()

        self.thread = threading.Thread(
            target=self._run,
            daemon=True
        )

    def start(self):
        self.thread.start()

    def stop(self):

        self.stop_event.set()

        self.thread.join(
            timeout=2
        )

    def _run(self):

        while not self.stop_event.wait(
            self.interval
        ):

            try:

                self.state["heartbeat_at"] = (
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                )

                save(
                    self.local_state,
                    self.drive,
                    self.state
                )

            except Exception as exc:

                log.warning(
                    "Heartbeat checkpoint failed: %s",
                    exc
                )


# ============================================================
# TOPIC INPUT
# ============================================================

def topics():

    path = ROOT / "input" / "topics.txt"

    if not path.exists():
        return []

    result = []

    for line in path.read_text(
        encoding="utf-8"
    ).splitlines():

        line = line.strip()

        if not line:
            continue

        if "|" not in line:
            continue

        book_id, topic = line.split(
            "|",
            1
        )

        book_id = book_id.strip()
        topic = topic.strip()

        if book_id and topic:

            result.append(
                (
                    book_id,
                    topic
                )
            )

    def sort_key(item):

        book_id = item[0]

        if book_id.isdigit():
            return (
                0,
                int(book_id)
            )

        return (
            1,
            book_id
        )

    return sorted(
        result,
        key=sort_key
    )


# ============================================================
# LOCAL WORK DIRECTORY
# ============================================================

def local(book_id):

    return (
        ROOT
        / "runtime"
        / book_id
    )


# ============================================================
# GEMINI PROMPTS
# ============================================================

def outline_prompt(topic):

    return f"""
You are the lead writer and curriculum architect for a
professional long-form educational audiobook.

TOPIC:
{topic}

Your task is to design the COMPLETE structure of the audiobook.

IMPORTANT REQUIREMENTS:

1. Create EXACTLY 12 chapters.
2. The audiobook must be designed for approximately
   60 to 90 minutes of natural spoken narration.
3. Target approximately 9,000 to 12,000 spoken words.
4. Do NOT write the complete chapters yet.
5. Create the complete chapter architecture first.
6. Every chapter must have a distinct purpose.
7. Chapters must logically build on one another.
8. Avoid repeating the same idea in different chapters.
9. The progression should feel like a transformation:
   problem → understanding → insight → method → application
   → obstacles → deeper understanding → action → transformation.
10. The book must feel like one coherent audiobook,
    not twelve unrelated articles.
11. Do not invent statistics.
12. Do not invent quotations.
13. Do not invent studies, experts or sources.
14. Do not make unsupported factual claims.
15. If a concept requires factual verification, phrase it
    carefully rather than inventing evidence.

RETENTION DESIGN:

The opening of the audiobook must immediately create a
reason for the listener to continue.

The opening should use one or more of:

- a painful relatable situation
- a powerful question
- a contradiction
- an uncomfortable truth
- a recognizable personal struggle
- a curiosity gap
- a future consequence

Do NOT use fake statistics or fake stories.

The final chapter should provide closure and a practical
transformation plan.

Return JSON ONLY.

Use exactly this structure:

{{
  "title": "professional audiobook title",
  "subtitle": "optional subtitle",
  "audience": "target listener",
  "central_transformation": "what changes for the listener",
  "core_promise": "what the listener should gain",
  "opening_hook_type": "pain/question/contradiction/truth/curiosity",
  "chapters": [
    {{
      "number": 1,
      "title": "...",
      "purpose": "...",
      "core_question": "...",
      "key_ideas": ["...", "...", "..."],
      "listener_takeaway": "...",
      "approx_minutes": 6,
      "approx_words": 850
    }}
  ]
}}

There MUST be exactly 12 chapter objects.

The approximate chapter lengths must collectively target
60 to 90 minutes of narration.

Return JSON only.
"""


def introduction_prompt(topic, outline):

    return f"""
You are writing the opening of a professional educational
audiobook.

TOPIC:
{topic}

TITLE:
{outline.get("title", topic)}

CENTRAL TRANSFORMATION:
{outline.get("central_transformation", "")}

CORE PROMISE:
{outline.get("core_promise", "")}

The listener has just pressed PLAY.

Your first responsibility is RETENTION.

Start with a strong opening that gives the listener a reason
to stay.

Choose the strongest appropriate opening device:

- a painful situation the listener recognizes
- a question that exposes a hidden problem
- a contradiction
- an uncomfortable truth
- a vivid but realistic situation
- a curiosity gap

Do not use fake statistics.
Do not invent quotations.
Do not pretend that a fictional story is a true story.

The opening should feel human, intelligent and natural.

After the hook:

1. Establish why the topic matters.
2. Make the listener feel understood.
3. Explain the central problem.
4. Create curiosity about the solution.
5. Introduce the journey of the audiobook.
6. Make the listener understand what they will gain
   by staying until the end.

Do NOT reveal every solution immediately.

Do NOT use headings.

Do NOT say:
"Welcome to this audiobook."

Do NOT say:
"In this chapter we will..."

Do NOT mention AI.

Do NOT mention this prompt.

Natural spoken English.

Target approximately 900 to 1,200 words.

Return narration only.
"""


def chapter_prompt(
    topic,
    outline,
    chapter,
    previous_summary
):

    number = chapter["number"]

    return f"""
You are writing Chapter {number} of a professional
long-form educational audiobook.

BOOK TITLE:
{outline.get("title", topic)}

TOPIC:
{topic}

CENTRAL TRANSFORMATION:
{outline.get("central_transformation", "")}

CORE PROMISE:
{outline.get("core_promise", "")}

CHAPTER NUMBER:
{number}

CHAPTER TITLE:
{chapter["title"]}

CHAPTER PURPOSE:
{chapter["purpose"]}

CHAPTER CORE QUESTION:
{chapter["core_question"]}

KEY IDEAS:
{json.dumps(chapter["key_ideas"], ensure_ascii=False)}

LISTENER TAKEAWAY:
{chapter["listener_takeaway"]}

TARGET:
Approximately {chapter["approx_minutes"]} minutes
and approximately {chapter["approx_words"]} words.

PREVIOUS CHAPTER CONTEXT:
{previous_summary}

WRITING RULES:

1. Write the COMPLETE chapter.
2. Make it sound natural when spoken aloud.
3. Build directly on the book's previous ideas.
4. Do not repeat previous chapters.
5. Do not refer to "the previous chapter".
6. Do not preview the entire future book.
7. Do not end with artificial cliffhangers.
8. Explain ideas clearly.
9. Use practical examples where useful.
10. Use smooth transitions.
11. Keep the listener engaged.
12. Alternate explanation, example, reflection and application.
13. Avoid repetitive motivational language.
14. Avoid generic filler.
15. Do not invent statistics.
16. Do not invent quotations.
17. Do not invent studies, experts or sources.
18. Do not fabricate personal experiences.
19. Do not mention AI.
20. Do not mention this prompt.
21. Do not use chapter headings inside the narration.
22. Do not write notes to the narrator.
23. Return ONLY the spoken narration.

The chapter should feel substantial enough for a
professional 60–90 minute audiobook.

Return narration only.
"""


# ============================================================
# FILE RESTORATION
# ============================================================

def ensure_local(
    drive,
    remote,
    path,
    validator
):

    if validator(path):
        return True

    try:

        drive.download_file_by_remote(
            remote,
            path
        )

        return validator(path)

    except FileNotFoundError:

        return False


# ============================================================
# DISK CHECK
# ============================================================

def disk_guard():

    usage = shutil.disk_usage(ROOT)

    free_gb = usage.free / (
        1024 ** 3
    )

    minimum = float(
        CONFIG.get(
            "disk_free_min_gb",
            3
        )
    )

    if free_gb < minimum:

        raise RuntimeError(
            f"Insufficient disk space: "
            f"{free_gb:.2f} GB free; "
            f"minimum {minimum:.2f} GB"
        )


# ============================================================
# MUSIC
# ============================================================

def find_music(
    drive,
    work
):

    music_dir = (
        work
        / "assets"
    )

    music_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    remote_root = (
        CONFIG["input_assets"]["drive_path"]
        + "/"
        + CONFIG["input_assets"]["music_folder"]
    )

    files = [
        item
        for item in drive.list_folder(
            remote_root
        )
        if item.get(
            "mimeType",
            ""
        ).startswith("audio/")
    ]

    if not files:
        return None

    chosen = files[0]

    path = (
        music_dir
        / chosen["name"]
    )

    if not valid_file(
        path,
        1024
    ):

        drive.download_file(
            chosen["id"],
            path
        )

    return path


# ============================================================
# VIDEO PLAN
# ============================================================

def choose_video_plan(
    drive,
    audio_duration,
    work
):

    cfg = CONFIG["video_library"]

    root = cfg["drive_path"]

    folders = [
        item
        for item in drive.list_folder(root)
        if item.get("mimeType")
        == "application/vnd.google-apps.folder"
    ]

    if not folders:

        raise FileNotFoundError(
            "No video folders found in Drive VIDEO_LIBRARY"
        )

    folders = sorted(
        folders,
        key=lambda item:
        item["name"].lower()
    )

    folder = (
        random.choice(folders)
        if cfg.get(
            "random_folder",
            True
        )
        else folders[0]
    )

    files = [
        item
        for item in drive.list_folder(
            root + "/" + folder["name"]
        )
        if item.get(
            "mimeType",
            ""
        ).startswith("video/")
    ]

    if not files:

        raise FileNotFoundError(
            f"No video clips found in Drive folder "
            f"{folder['name']}"
        )

    if cfg.get(
        "random_clip_order",
        True
    ):

        random.shuffle(files)

    chosen = []
    total = 0.0

    library_dir = (
        work
        / "video"
        / "library"
    )

    library_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    durations = {}

    for item in files:

        disk_guard()

        local_clip = (
            library_dir
            / item["name"]
        )

        if not valid_media(
            local_clip,
            "video"
        ):

            drive.download_file(
                item["id"],
                local_clip
            )

        d = duration(
            local_clip
        )

        durations[
            item["id"]
        ] = d

        chosen.append(
            {
                "id": item["id"],
                "name": item["name"]
            }
        )

        total += d

        if total >= audio_duration:
            break

    if total < audio_duration:

        if not cfg.get(
            "allow_clip_duplication",
            True
        ):

            raise RuntimeError(
                f"Video library duration is insufficient "
                f"({total:.1f}s < {audio_duration:.1f}s)"
            )

        if not chosen:

            raise RuntimeError(
                "No usable video clips available."
            )

        while total < audio_duration:

            item = random.choice(
                chosen
            )

            chosen.append(item)

            total += durations[
                item["id"]
            ]

    if cfg.get(
        "random_clip_order",
        True
    ):

        random.shuffle(chosen)

    return {
        "folder_id": folder["id"],
        "folder": folder["name"],
        "clips": chosen
    }


# ============================================================
# MAIN PIPELINE
# ============================================================

def main():

    sec = secret_config()

    retry = Retry(
        CONFIG["retry_count"],
        CONFIG["retry_delay_min"],
        CONFIG["retry_delay_max"]
    )

    drive = Drive(
        sec["google_drive_credentials"],
        retry
    )

    gem = Gemini(
        sec["gemini_api_key"],
        retry,
        CONFIG,
        VOICE
    )

    tts = TTS(
        retry,
        VOICE
    )

    yt = YouTube(
        sec["youtube_client_id"],
        sec["youtube_client_secret"],
        sec["youtube_refresh_token"],
        retry
    )

    # --------------------------------------------------------
    # REQUIRED DRIVE FOLDERS
    # --------------------------------------------------------

    for folder in (
        "STATE",
        "WORK",
        "SUCCESS",
        "FAILED"
    ):

        drive.folder_path(folder)

    # --------------------------------------------------------
    # DAILY QUEUE
    #
    # First unfinished topic wins.
    #
    # SUCCESS is skipped.
    # FAILED is retried.
    # PARTIAL work is resumed.
    # --------------------------------------------------------

    selected = None

    for book_id, topic in topics():

        work = local(
            book_id
        )

        work.mkdir(
            parents=True,
            exist_ok=True
        )

        state = load(
            work / "state.json",
            drive,
            book_id
        )

        if state and state.get(
            "status"
        ) == "SUCCESS":

            continue

        selected = (
            book_id,
            topic,
            state,
            work
        )

        break

    if not selected:

        log.info(
            "No unfinished books in queue."
        )

        return

    book_id, topic, state, work = selected

    state = state or default(
        book_id,
        topic
    )

    # --------------------------------------------------------
    # RESUME
    # --------------------------------------------------------

    state["topic"] = topic
    state["status"] = "RUNNING"

    save(
        work / "state.json",
        drive,
        state
    )

    heartbeat = Heartbeat(
        work / "state.json",
        drive,
        state
    )

    heartbeat.start()

    try:

        disk_guard()

        for folder in (
            "chapters",
            "audio",
            "video",
            "thumbnail",
            "script"
        ):

            (
                work / folder
            ).mkdir(
                parents=True,
                exist_ok=True
            )

        # ====================================================
        # STEP 1 — 12 CHAPTER OUTLINE
        # ====================================================

        outline_path = (
            work
            / "outline.json"
        )

        if not ensure_local(
            drive,
            f"WORK/{book_id}/outline.json",
            outline_path,
            valid_file
        ):

            outline = gem.json(
                "script",
                outline_prompt(topic)
            )

            chapters = outline.get(
                "chapters",
                []
            )

            if len(chapters) != 12:

                raise RuntimeError(
                    "Gemini outline QC failed: "
                    f"expected exactly 12 chapters, "
                    f"received {len(chapters)}."
                )

            for index, chapter in enumerate(
                chapters,
                start=1
            ):

                if int(
                    chapter["number"]
                ) != index:

                    raise RuntimeError(
                        "Gemini outline chapter numbering "
                        "is invalid."
                    )

            outline_path.write_text(
                json.dumps(
                    outline,
                    indent=2,
                    ensure_ascii=False
                ),
                encoding="utf-8"
            )

            drive.put_file(
                outline_path,
                f"WORK/{book_id}/outline.json"
            )

        else:

            outline = json.loads(
                outline_path.read_text(
                    encoding="utf-8"
                )
            )

        chapters = outline.get(
            "chapters",
            []
        )

        if len(chapters) != 12:

            raise RuntimeError(
                "Stored outline does not contain exactly "
                "12 chapters."
            )

        state["chapter_count"] = 12
        state["current_stage"] = (
            "OUTLINE_COMPLETE"
        )

        save(
            work / "state.json",
            drive,
            state
        )

        # ====================================================
        # STEP 2 — INTRODUCTION
        # ====================================================

        intro = (
            work
            / "chapters"
            / "00_intro.txt"
        )

        if not ensure_local(
            drive,
            f"WORK/{book_id}/chapters/00_intro.txt",
            intro,
            valid_file
        ):

            intro_text = gem.text(
                "script",
                introduction_prompt(
                    topic,
                    outline
                )
            )

            if len(
                intro_text.split()
            ) < 700:

                raise RuntimeError(
                    "Introduction is too short."
                )

            intro.write_text(
                intro_text.strip(),
                encoding="utf-8"
            )

            drive.put_file(
                intro,
                f"WORK/{book_id}/chapters/00_intro.txt"
            )

        state["current_stage"] = (
            "INTRO_COMPLETE"
        )

        save(
            work / "state.json",
            drive,
            state
        )

        # ====================================================
        # STEP 3 — ONE CHAPTER AT A TIME
        # ====================================================

        for chapter in chapters:

            number = int(
                chapter["number"]
            )

            chapter_file = (
                work
                / "chapters"
                / f"{number:02d}.txt"
            )

            remote_chapter = (
                f"WORK/{book_id}/chapters/"
                f"{number:02d}.txt"
            )

            # ------------------------------------------------
            # ALREADY DONE?
            # ------------------------------------------------

            if ensure_local(
                drive,
                remote_chapter,
                chapter_file,
                valid_file
            ):

                state[
                    "last_completed_chapter"
                ] = max(
                    state.get(
                        "last_completed_chapter",
                        -1
                    ),
                    number
                )

                save(
                    work / "state.json",
                    drive,
                    state
                )

                continue

            # ------------------------------------------------
            # BUILD PREVIOUS CONTEXT
            # ------------------------------------------------

            previous_parts = [
                intro
            ]

            for previous_chapter in chapters:

                previous_number = int(
                    previous_chapter["number"]
                )

                if previous_number >= number:
                    break

                previous_file = (
                    work
                    / "chapters"
                    / f"{previous_number:02d}.txt"
                )

                if previous_file.exists():

                    previous_parts.append(
                        previous_file
                    )

            previous_text = "\n\n".join(
                path.read_text(
                    encoding="utf-8"
                )
                for path in previous_parts
                if path.exists()
            )

            # Limit context sent to Gemini.
            previous_context = (
                previous_text[-50000:]
            )

            # ------------------------------------------------
            # GENERATE THIS CHAPTER ONLY
            # ------------------------------------------------

            log.info(
                "Generating chapter %s/12: %s",
                number,
                chapter["title"]
            )

            chapter_text = gem.text(
                "script",
                chapter_prompt(
                    topic,
                    outline,
                    chapter,
                    previous_context
                )
            )

            chapter_text = (
                chapter_text
                .strip()
            )

            word_count = len(
                chapter_text.split()
            )

            expected_words = int(
                chapter.get(
                    "approx_words",
                    800
                )
            )

            minimum_words = int(
                expected_words * 0.70
            )

            if word_count < minimum_words:

                raise RuntimeError(
                    f"Chapter {number} is too short: "
                    f"{word_count} words; "
                    f"expected around {expected_words}."
                )

            if re.search(
                r"\b("
                r"TODO|"
                r"TBD|"
                r"PLACEHOLDER|"
                r"INSERT .* HERE"
                r")\b",
                chapter_text,
                re.IGNORECASE
            ):

                raise RuntimeError(
                    f"Chapter {number} contains "
                    "unfinished placeholder text."
                )

            chapter_file.write_text(
                chapter_text,
                encoding="utf-8"
            )

            # ------------------------------------------------
            # IMMEDIATE CHECKPOINT
            # ------------------------------------------------

            drive.put_file(
                chapter_file,
                remote_chapter
            )

            state[
                "last_completed_chapter"
            ] = number

            state["current_stage"] = (
                f"CHAPTER_{number:02d}_COMPLETE"
            )

            save(
                work / "state.json",
                drive,
                state
            )

            log.info(
                "Chapter %s completed and checkpointed.",
                number
            )

        # ====================================================
        # STEP 4 — FINAL SCRIPT
        # ====================================================

        final_script = (
            work
            / "script"
            / "final_script.txt"
        )

        remote_final = (
            f"WORK/{book_id}/script/"
            "final_script.txt"
        )

        if not ensure_local(
            drive,
            remote_final,
            final_script,
            valid_file
        ):

            ordered_files = [
                intro
            ]

            for chapter in chapters:

                ordered_files.append(
                    work
                    / "chapters"
                    / f"{int(chapter['number']):02d}.txt"
                )

            final_text = "\n\n".join(
                path.read_text(
                    encoding="utf-8"
                ).strip()
                for path in ordered_files
            )

            final_script.write_text(
                final_text,
                encoding="utf-8"
            )

            drive.put_file(
                final_script,
                remote_final
            )

        script_text = (
            final_script.read_text(
                encoding="utf-8"
            )
        )

        word_count = len(
            script_text.split()
        )

        minimum_words = int(
            CONFIG["script_minutes_min"]
            * CONFIG["words_per_minute_min"]
        )

        maximum_words = int(
            CONFIG["script_minutes_max"]
            * CONFIG["words_per_minute_max"]
        )

        if not (
            minimum_words
            <= word_count
            <= maximum_words
        ):

            raise RuntimeError(
                f"Script length QC failed: "
                f"{word_count} words. "
                f"Expected {minimum_words}–"
                f"{maximum_words} words."
            )

        if re.search(
            r"\b("
            r"TODO|"
            r"TBD|"
            r"PLACEHOLDER|"
            r"INSERT .* HERE"
            r")\b",
            script_text,
            re.IGNORECASE
        ):

            raise RuntimeError(
                "Final script contains "
                "unfinished placeholder text."
            )

        state["current_stage"] = (
            "SCRIPT_COMPLETE"
        )

        save(
            work / "state.json",
            drive,
            state
        )

        # ====================================================
        # EVERYTHING BELOW HERE
        # REMAINS YOUR EXISTING PIPELINE
        #
        # TTS
        # AUDIO
        # VIDEO
        # THUMBNAIL
        # YOUTUBE
        #
        # Keep the existing code from your current main.py
        # starting at the TTS chunk generation section.
        # ====================================================

        raise RuntimeError(
            "SCRIPT PIPELINE COMPLETE. "
            "Restore the existing TTS/video/YouTube "
            "section below this point."
        )

    except Exception as exc:

        error_class = classify_error(
            exc
        )

        state["error"] = str(exc)

        state["error_class"] = (
            error_class
        )

        state["status"] = (
            "WAITING_FOR_QUOTA"
            if error_class
            == "QUOTA_EXCEEDED"
            else "FAILED"
        )

        save(
            work / "state.json",
            drive,
            state
        )

        error_log = (
            work
            / "error.log"
        )

        error_log.write_text(
            f"{error_class}: {exc}\n",
            encoding="utf-8"
        )

        try:

            drive.put_file(
                work / "state.json",
                f"FAILED/{book_id}/state.json"
            )

            drive.put_file(
                error_log,
                f"FAILED/{book_id}/error.log"
            )

        except Exception as save_error:

            log.error(
                "Could not persist failure diagnostics: %s",
                save_error
            )

        raise

    finally:

        heartbeat.stop()


if __name__ == "__main__":
    main()
