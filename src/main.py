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


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("audiobook")


# ============================================================
# GEMINI DEFAULT MODELS
# ============================================================
#
# The repository config may not contain a "models" section.
# Add safe defaults here so the system does not crash simply
# because that section is absent.
#
# If config.json later contains its own models section,
# those values are used instead.
# ============================================================

CONFIG.setdefault(
    "models",
    {
        "script": [
            "gemini-2.5-flash"
        ],
        "metadata": [
            "gemini-2.5-flash"
        ]
    }
)


# ============================================================
# ERROR CLASSIFICATION
# ============================================================

def classify_error(exc):

    status = getattr(
        exc,
        "status_code",
        None
    )

    if status is None and isinstance(
        exc,
        HttpError
    ):
        status = getattr(
            exc.resp,
            "status",
            None
        )

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

    if status in (
        429,
        500,
        502,
        503,
        504
    ) or any(
        x in text
        for x in (
            "rate limit",
            "quota exceeded",
            "dailylimit",
            "temporarily unavailable",
            "timeout"
        )
    ):

        if (
            "quota" in text
            or "dailylimit" in text
        ):
            return "QUOTA_EXCEEDED"

        return "TEMPORARY"

    if any(
        x in text
        for x in (
            "corrupt",
            "invalid data"
        )
    ):
        return "CORRUPT_FILE"

    if isinstance(
        exc,
        (
            FileNotFoundError,
            ValueError
        )
    ):
        return "PERMANENT"

    return "TEMPORARY"


# ============================================================
# RETRY ENGINE
# ============================================================

class Retry:

    def __init__(
        self,
        n,
        lo,
        hi
    ):

        self.n = int(n)
        self.lo = float(lo)
        self.hi = float(hi)

    def __call__(
        self,
        fn,
        provider,
        model=None
    ):

        last = None

        for attempt in range(
            1,
            self.n + 1
        ):

            try:

                log.info(
                    "PROVIDER=%s MODEL=%s ATTEMPT=%s/%s",
                    provider,
                    model or "-",
                    attempt,
                    self.n
                )

                return fn()

            except Exception as exc:

                last = exc

                error_class = classify_error(
                    exc
                )

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

        self.stop_event = (
            threading.Event()
        )

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

                self.state[
                    "heartbeat_at"
                ] = datetime.now(
                    timezone.utc
                ).isoformat()

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
# TOPIC QUEUE
# ============================================================

def topics():

    path = (
        ROOT
        / "input"
        / "topics.txt"
    )

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
# LOCAL BOOK DIRECTORY
# ============================================================

def local(book_id):

    return (
        ROOT
        / "runtime"
        / book_id
    )


# ============================================================
# PROMPT — COMPLETE 12 CHAPTER OUTLINE
# ============================================================

def outline_prompt(topic):

    return f"""
You are the lead writer and curriculum architect for a
professional long-form educational audiobook.

TOPIC:
{topic}

Create the complete architecture for ONE original audiobook.

CORE REQUIREMENTS:

1. EXACTLY 12 chapters.
2. Target final narration length: 60–90 minutes.
3. Target approximately 7,200–13,500 spoken words.
4. The book must feel like one coherent journey.
5. Every chapter must have a distinct purpose.
6. Do not repeat the same concept across chapters.
7. Build a logical transformation from problem to solution.
8. The final chapter must provide practical closure and
   an actionable implementation plan.
9. Do not invent statistics.
10. Do not invent quotations.
11. Do not invent studies.
12. Do not invent experts or sources.
13. Do not present fictional stories as real events.

RETENTION ARCHITECTURE:

The opening must create a strong reason to continue listening.

The hook may use:

- a painful relatable problem
- an uncomfortable truth
- a powerful question
- a contradiction
- a recognizable struggle
- a curiosity gap
- a consequence the listener has not considered

The opening should create tension and curiosity without
using fake statistics or sensational claims.

CHAPTER PROGRESSION:

The 12 chapters should generally progress through:

1. Recognition of the problem
2. Understanding why the problem exists
3. Hidden mechanisms
4. First major shift
5. Practical method
6. Implementation
7. Common obstacles
8. Deeper principles
9. Real-world application
10. Long-term consistency
11. Advanced understanding
12. Transformation and action plan

Do not mechanically follow that list if another structure
better fits the topic, but preserve a meaningful progression.

RETURN JSON ONLY.

Use exactly this structure:

{{
  "title": "final audiobook title",
  "subtitle": "optional subtitle",
  "audience": "target listener",
  "central_transformation": "what changes for the listener",
  "core_promise": "what the listener will gain",
  "opening_hook_type": "pain/question/contradiction/truth/curiosity",
  "chapters": [
    {{
      "number": 1,
      "title": "chapter title",
      "purpose": "specific purpose",
      "core_question": "question this chapter answers",
      "key_ideas": [
        "idea one",
        "idea two",
        "idea three"
      ],
      "listener_takeaway": "what the listener should understand",
      "approx_minutes": 7,
      "approx_words": 900
    }}
  ]
}}

There MUST be exactly 12 chapter objects.

Chapter numbers MUST be exactly:

1,2,3,4,5,6,7,8,9,10,11,12.

The combined approximate word counts must target
7,200–13,500 words.

Return JSON only.
"""


# ============================================================
# PROMPT — INTRODUCTION
# ============================================================

def introduction_prompt(
    topic,
    outline
):

    return f"""
You are writing the opening of a professional educational
audiobook.

BOOK TITLE:
{outline.get("title", topic)}

TOPIC:
{topic}

CENTRAL TRANSFORMATION:
{outline.get("central_transformation", "")}

CORE PROMISE:
{outline.get("core_promise", "")}

The listener has just pressed PLAY.

Your FIRST responsibility is retention.

Start immediately with a powerful reason to keep listening.

Use the most suitable opening:

- a painful situation
- a powerful question
- an uncomfortable truth
- a contradiction
- a recognizable struggle
- a curiosity gap
- a meaningful consequence

The opening must feel human and psychologically relevant.

Do NOT use:

- fake statistics
- fake studies
- fake quotations
- fake experts
- invented research
- exaggerated promises
- clickbait claims

After the hook:

1. Make the listener feel understood.
2. Explain the central problem.
3. Show why the problem matters.
4. Create curiosity about the solution.
5. Establish the transformation promised by the book.
6. Make the listener want to hear the entire journey.

Do not reveal every solution immediately.

Do not use headings.

Do not say:

"Welcome to this audiobook."

Do not say:

"In this chapter we will..."

Do not mention AI.

Do not mention this prompt.

Use natural spoken English.

Target approximately 900–1,200 words.

Return ONLY the narration.
"""


# ============================================================
# PROMPT — ONE CHAPTER
# ============================================================

def chapter_prompt(
    topic,
    outline,
    chapter,
    previous
):

    return f"""
You are writing ONE chapter of a professional
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
{chapter["number"]}

CHAPTER TITLE:
{chapter["title"]}

CHAPTER PURPOSE:
{chapter["purpose"]}

CHAPTER CORE QUESTION:
{chapter["core_question"]}

KEY IDEAS:
{json.dumps(
    chapter["key_ideas"],
    ensure_ascii=False
)}

LISTENER TAKEAWAY:
{chapter["listener_takeaway"]}

TARGET LENGTH:
Approximately {chapter["approx_minutes"]} minutes.

TARGET WORD COUNT:
Approximately {chapter["approx_words"]} words.

PREVIOUS NARRATION CONTEXT:
{previous}

WRITING REQUIREMENTS:

1. Write the COMPLETE chapter.
2. Make it sound natural when spoken aloud.
3. Build on the established book architecture.
4. Introduce new ideas rather than repeating earlier ones.
5. Use clear explanations.
6. Use practical examples where useful.
7. Use reflection when useful.
8. Use smooth transitions.
9. Maintain listener curiosity.
10. Maintain conceptual continuity.
11. Do not reveal future chapters unnecessarily.
12. Do not refer to "the previous chapter".
13. Do not refer to "the next chapter".
14. Do not use artificial cliffhangers.
15. Do not use generic filler.
16. Do not repeatedly say "imagine this".
17. Do not invent statistics.
18. Do not invent quotations.
19. Do not invent studies.
20. Do not invent experts.
21. Do not fabricate personal experiences.
22. Do not mention AI.
23. Do not mention this prompt.
24. Do not include narrator instructions.
25. Do not include production notes.
26. Do not use chapter headings inside the narration.
27. Return ONLY spoken narration.

The chapter must be substantial and useful.

Return narration only.
"""


# ============================================================
# VALIDATE OUTLINE
# ============================================================

def validate_outline(
    outline
):

    if not isinstance(
        outline,
        dict
    ):
        raise ValueError(
            "Gemini outline is not a JSON object."
        )

    chapters = outline.get(
        "chapters"
    )

    if not isinstance(
        chapters,
        list
    ):
        raise ValueError(
            "Gemini outline does not contain a chapters array."
        )

    if len(chapters) != 12:

        raise ValueError(
            "Gemini outline must contain exactly "
            f"12 chapters; received {len(chapters)}."
        )

    for expected, chapter in enumerate(
        chapters,
        start=1
    ):

        if not isinstance(
            chapter,
            dict
        ):
            raise ValueError(
                f"Chapter {expected} is invalid."
            )

        if int(
            chapter.get(
                "number",
                -1
            )
        ) != expected:

            raise ValueError(
                f"Chapter numbering error at chapter {expected}."
            )

        for key in (
            "title",
            "purpose",
            "core_question",
            "key_ideas",
            "listener_takeaway"
        ):

            if not chapter.get(key):

                raise ValueError(
                    f"Chapter {expected} missing {key}."
                )

        if not isinstance(
            chapter["key_ideas"],
            list
        ):

            raise ValueError(
                f"Chapter {expected} key_ideas must be a list."
            )

        if not chapter.get(
            "approx_words"
        ):

            chapter["approx_words"] = 850

        if not chapter.get(
            "approx_minutes"
        ):

            chapter["approx_minutes"] = 7

    return outline


# ============================================================
# VALIDATE GENERATED NARRATION
# ============================================================

def validate_narration(
    text,
    minimum_words,
    label
):

    if not text:
        raise RuntimeError(
            f"{label} returned empty narration."
        )

    text = text.strip()

    word_count = len(
        text.split()
    )

    if word_count < minimum_words:

        raise RuntimeError(
            f"{label} is too short: "
            f"{word_count} words; minimum "
            f"{minimum_words}."
        )

    if re.search(
        r"\b("
        r"TODO|"
        r"TBD|"
        r"PLACEHOLDER|"
        r"INSERT .* HERE|"
        r"\[WRITE .*?\]"
        r")\b",
        text,
        re.IGNORECASE
    ):

        raise RuntimeError(
            f"{label} contains unfinished placeholder text."
        )

    return text


# ============================================================
# REMOTE → LOCAL RESTORE
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
# DISK SPACE
# ============================================================

def disk_guard():

    usage = shutil.disk_usage(
        ROOT
    )

    free_gb = (
        usage.free
        / (1024 ** 3)
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
        CONFIG[
            "input_assets"
        ]["drive_path"]
        + "/"
        + CONFIG[
            "input_assets"
        ]["music_folder"]
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

    cfg = CONFIG[
        "video_library"
    ]

    root = cfg[
        "drive_path"
    ]

    folders = [
        item
        for item in drive.list_folder(
            root
        )
        if item.get(
            "mimeType"
        )
        == "application/vnd.google-apps.folder"
    ]

    if not folders:

        raise FileNotFoundError(
            "No video folders found in Drive VIDEO_LIBRARY."
        )

    folders = sorted(
        folders,
        key=lambda x:
        x["name"].lower()
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
            root
            + "/"
            + folder["name"]
        )
        if item.get(
            "mimeType",
            ""
        ).startswith("video/")
    ]

    if not files:

        raise FileNotFoundError(
            f"No video clips found in "
            f"Drive folder {folder['name']}."
        )

    if cfg.get(
        "random_clip_order",
        True
    ):

        random.shuffle(
            files
        )

    chosen = []
    total = 0.0
    durations = {}

    library_dir = (
        work
        / "video"
        / "library"
    )

    library_dir.mkdir(
        parents=True,
        exist_ok=True
    )

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
                "Video library duration is insufficient."
            )

        if not chosen:

            raise RuntimeError(
                "No usable video clips available."
            )

        while total < audio_duration:

            item = random.choice(
                chosen
            )

            chosen.append(
                item
            )

            total += durations[
                item["id"]
            ]

    if cfg.get(
        "random_clip_order",
        True
    ):

        random.shuffle(
            chosen
        )

    return {
        "folder_id": folder["id"],
        "folder": folder["name"],
        "clips": chosen
    }


# ============================================================
# DOWNLOAD VIDEO PLAN
# ============================================================

def download_plan_clips(
    drive,
    plan,
    work
):

    library_dir = (
        work
        / "video"
        / "library"
    )

    library_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    paths = []

    for item in plan[
        "clips"
    ]:

        path = (
            library_dir
            / item["name"]
        )

        if not valid_media(
            path,
            "video"
        ):

            drive.download_file(
                item["id"],
                path
            )

        if not valid_media(
            path,
            "video"
        ):

            raise RuntimeError(
                "Video clip validation failed: "
                + item["name"]
            )

        paths.append(
            path
        )

    return paths


# ============================================================
# LOCAL CLEANUP
# ============================================================

def cleanup_local_large(
    work
):

    for name in (
        "video",
        "audio"
    ):

        path = (
            work
            / name
        )

        if path.exists():

            shutil.rmtree(
                path,
                ignore_errors=True
            )


# ============================================================
# MAIN PIPELINE
# ============================================================

def main():

    # --------------------------------------------------------
    # SECRETS
    # --------------------------------------------------------

    sec = secret_config()

    # --------------------------------------------------------
    # RETRY ENGINE
    # --------------------------------------------------------

    retry = Retry(
        CONFIG[
            "retry_count"
        ],
        CONFIG[
            "retry_delay_min"
        ],
        CONFIG[
            "retry_delay_max"
        ]
    )

    # --------------------------------------------------------
    # SERVICES
    # --------------------------------------------------------

    drive = Drive(
        sec[
            "google_drive_credentials"
        ],
        retry
    )

    gem = Gemini(
        sec[
            "gemini_api_key"
        ],
        retry,
        CONFIG,
        VOICE
    )

    tts = TTS(
        retry,
        VOICE
    )

    yt = YouTube(
        sec[
            "youtube_client_id"
        ],
        sec[
            "youtube_client_secret"
        ],
        sec[
            "youtube_refresh_token"
        ],
        retry
    )

    # --------------------------------------------------------
    # DRIVE ROOT FOLDERS
    # --------------------------------------------------------

    for folder in (
        "STATE",
        "WORK",
        "SUCCESS",
        "FAILED"
    ):

        drive.folder_path(
            folder
        )

    # --------------------------------------------------------
    # DAILY QUEUE
    #
    # First unfinished book is selected.
    #
    # SUCCESS:
    #     skip permanently
    #
    # FAILED:
    #     retry
    #
    # PARTIAL:
    #     resume
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

        if (
            state
            and state.get(
                "status"
            ) == "SUCCESS"
        ):

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
            "No eligible book in queue."
        )

        return

    book_id, topic, state, work = (
        selected
    )

    # --------------------------------------------------------
    # STATE
    # --------------------------------------------------------

    state = (
        state
        or default(
            book_id,
            topic
        )
    )

    state[
        "topic"
    ] = topic

    state[
        "status"
    ] = "RUNNING"

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

        # ----------------------------------------------------
        # LOCAL DIRECTORIES
        # ----------------------------------------------------

        for folder in (
            "chapters",
            "audio",
            "video",
            "thumbnail",
            "script",
            "assets"
        ):

            (
                work / folder
            ).mkdir(
                parents=True,
                exist_ok=True
            )

        # ====================================================
        # STEP 1
        # GENERATE / RESTORE 12-CHAPTER OUTLINE
        # ====================================================

        outline_path = (
            work
            / "outline.json"
        )

        outline_remote = (
            f"WORK/{book_id}/outline.json"
        )

        if not ensure_local(
            drive,
            outline_remote,
            outline_path,
            valid_file
        ):

            log.info(
                "Generating 12-chapter outline for %s",
                book_id
            )

            outline = gem.json(
                "script",
                outline_prompt(
                    topic
                )
            )

            outline = validate_outline(
                outline
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
                outline_remote
            )

        else:

            outline = json.loads(
                outline_path.read_text(
                    encoding="utf-8"
                )
            )

            outline = validate_outline(
                outline
            )

        chapters = outline[
            "chapters"
        ]

        state[
            "chapter_count"
        ] = 12

        state[
            "current_stage"
        ] = "OUTLINE_COMPLETE"

        save(
            work / "state.json",
            drive,
            state
        )

        # ====================================================
        # STEP 2
        # INTRODUCTION / RETENTION HOOK
        # ====================================================

        intro = (
            work
            / "chapters"
            / "00_intro.txt"
        )

        intro_remote = (
            f"WORK/{book_id}/chapters/"
            "00_intro.txt"
        )

        if not ensure_local(
            drive,
            intro_remote,
            intro,
            valid_file
        ):

            log.info(
                "Generating audiobook introduction."
            )

            intro_text = gem.text(
                "script",
                introduction_prompt(
                    topic,
                    outline
                )
            )

            intro_text = validate_narration(
                intro_text,
                700,
                "Introduction"
            )

            intro.write_text(
                intro_text,
                encoding="utf-8"
            )

            drive.put_file(
                intro,
                intro_remote
            )

        state[
            "current_stage"
        ] = "INTRO_COMPLETE"

        save(
            work / "state.json",
            drive,
            state
        )

        # ====================================================
        # STEP 3
        # WRITE ONE CHAPTER AT A TIME
        #
        # EVERY COMPLETED CHAPTER IS UPLOADED IMMEDIATELY.
        #
        # If GitHub Actions stops here:
        #
        # next run
        #     ↓
        # existing chapters restored
        #     ↓
        # missing chapter generated
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

            chapter_remote = (
                f"WORK/{book_id}/chapters/"
                f"{number:02d}.txt"
            )

            # ------------------------------------------------
            # ALREADY COMPLETE?
            # ------------------------------------------------

            if ensure_local(
                drive,
                chapter_remote,
                chapter_file,
                valid_file
            ):

                state[
                    "last_completed_chapter"
                ] = max(
                    int(
                        state.get(
                            "last_completed_chapter",
                            -1
                        )
                    ),
                    number
                )

                save(
                    work / "state.json",
                    drive,
                    state
                )

                log.info(
                    "Chapter %02d already complete.",
                    number
                )

                continue

            # ------------------------------------------------
            # PREVIOUS CONTEXT
            # ------------------------------------------------

            previous_parts = [
                intro
            ]

            for previous_chapter in chapters:

                previous_number = int(
                    previous_chapter[
                        "number"
                    ]
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

            # Keep prompt size controlled.
            previous_context = (
                previous_text[-50000:]
            )

            # ------------------------------------------------
            # GENERATE CHAPTER
            # ------------------------------------------------

            log.info(
                "Generating chapter %02d/12: %s",
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

            expected_words = int(
                chapter.get(
                    "approx_words",
                    850
                )
            )

            minimum_words = max(
                600,
                int(
                    expected_words
                    * 0.70
                )
            )

            chapter_text = validate_narration(
                chapter_text,
                minimum_words,
                f"Chapter {number}"
            )

            # ------------------------------------------------
            # SAVE LOCALLY
            # ------------------------------------------------

            chapter_file.write_text(
                chapter_text,
                encoding="utf-8"
            )

            # ------------------------------------------------
            # IMMEDIATE DRIVE CHECKPOINT
            # ------------------------------------------------

            drive.put_file(
                chapter_file,
                chapter_remote
            )

            state[
                "last_completed_chapter"
            ] = number

            state[
                "current_stage"
            ] = (
                f"CHAPTER_{number:02d}_COMPLETE"
            )

            save(
                work / "state.json",
                drive,
                state
            )

            log.info(
                "Chapter %02d completed and checkpointed.",
                number
            )

        # ====================================================
        # STEP 4
        # BUILD FINAL SCRIPT
        # ====================================================

        final_script = (
            work
            / "script"
            / "final_script.txt"
        )

        final_remote = (
            f"WORK/{book_id}/script/"
            "final_script.txt"
        )

        if not ensure_local(
            drive,
            final_remote,
            final_script,
            valid_file
        ):

            ordered = [
                intro
            ]

            for chapter in chapters:

                ordered.append(
                    work
                    / "chapters"
                    / (
                        f"{int(chapter['number']):02d}.txt"
                    )
                )

            missing = [
                str(path)
                for path in ordered
                if not path.exists()
            ]

            if missing:

                raise FileNotFoundError(
                    "Cannot assemble final script. "
                    "Missing: "
                    + ", ".join(
                        missing
                    )
                )

            final_text = "\n\n".join(
                path.read_text(
                    encoding="utf-8"
                ).strip()
                for path in ordered
            )

            final_script.write_text(
                final_text,
                encoding="utf-8"
            )

            drive.put_file(
                final_script,
                final_remote
            )

        # ----------------------------------------------------
        # FINAL SCRIPT QC
        # ----------------------------------------------------

        script_text = (
            final_script.read_text(
                encoding="utf-8"
            )
        )

        word_count = len(
            script_text.split()
        )

        minimum_words = int(
            CONFIG[
                "script_minutes_min"
            ]
            * CONFIG[
                "words_per_minute_min"
            ]
        )

        maximum_words = int(
            CONFIG[
                "script_minutes_max"
            ]
            * CONFIG[
                "words_per_minute_max"
            ]
        )

        if (
            word_count < minimum_words
            or word_count > maximum_words
        ):

            raise RuntimeError(
                "Final script length QC failed: "
                f"{word_count} words. "
                f"Expected "
                f"{minimum_words}–"
                f"{maximum_words} words."
            )

        if re.search(
            r"\b("
            r"TODO|"
            r"TBD|"
            r"PLACEHOLDER|"
            r"INSERT .* HERE|"
            r"\[WRITE .*?\]"
            r")\b",
            script_text,
            re.IGNORECASE
        ):

            raise RuntimeError(
                "Final script contains "
                "unfinished placeholder text."
            )

        state[
            "current_stage"
        ] = "SCRIPT_COMPLETE"

        save(
            work / "state.json",
            drive,
            state
        )

        log.info(
            "Final script completed: %s words.",
            word_count
        )

        # ====================================================
        # STEP 5
        # SPLIT SCRIPT FOR TTS
        # ====================================================

        words = script_text.split()

        chunk_words = 900

        chunks = [
            " ".join(
                words[
                    i:i + chunk_words
                ]
            )
            for i in range(
                0,
                len(words),
                chunk_words
            )
        ]

        state[
            "total_chunks"
        ] = len(chunks)

        save(
            work / "state.json",
            drive,
            state
        )

        # ====================================================
        # STEP 6
        # TTS
        #
        # Existing TTS implementation is preserved.
        # Edge Neural / Kokoro fallback remains controlled
        # by src/tts.py and voice.json.
        # ====================================================

        provider = state.get(
            "tts_provider"
        )

        for index, text in enumerate(
            chunks,
            start=1
        ):

            disk_guard()

            output = (
                work
                / "audio"
                / f"{index:04d}.wav"
            )

            remote_audio = (
                f"WORK/{book_id}/audio/"
                f"{index:04d}.wav"
            )

            if ensure_local(
                drive,
                remote_audio,
                output,
                lambda p:
                    valid_media(
                        p,
                        "audio"
                    )
            ):

                state[
                    "completed_chunks"
                ] = sorted(
                    set(
                        state.get(
                            "completed_chunks",
                            []
                        )
                        + [index]
                    )
                )

                save(
                    work / "state.json",
                    drive,
                    state
                )

                continue

            used_provider = tts.make(
                text,
                output,
                provider
                if CONFIG.get(
                    "allow_provider_switch_within_book",
                    True
                )
                else None
            )

            provider = used_provider

            state[
                "tts_provider"
            ] = provider

            state[
                "completed_chunks"
            ] = sorted(
                set(
                    state.get(
                        "completed_chunks",
                        []
                    )
                    + [index]
                )
            )

            drive.put_file(
                output,
                remote_audio
            )

            save(
                work / "state.json",
                drive,
                state
            )

        # ====================================================
        # STEP 7
        # FINAL AUDIO
        # ====================================================

        audio = (
            work
            / "audio"
            / "final_audio.wav"
        )

        remote_audio = (
            f"WORK/{book_id}/audio/"
            "final_audio.wav"
        )

        if not ensure_local(
            drive,
            remote_audio,
            audio,
            lambda p:
                valid_media(
                    p,
                    "audio"
                )
        ):

            audio_chunks = [
                work
                / "audio"
                / f"{i:04d}.wav"
                for i in range(
                    1,
                    len(chunks) + 1
                )
            ]

            for path in audio_chunks:

                if not valid_media(
                    path,
                    "audio"
                ):

                    raise FileNotFoundError(
                        "Missing audio chunk: "
                        + path.name
                    )

            assemble_audio(
                audio_chunks,
                audio
            )

            if not valid_media(
                audio,
                "audio"
            ):

                raise RuntimeError(
                    "Final audio validation failed."
                )

            drive.put_file(
                audio,
                remote_audio
            )

        state[
            "audio_completed"
        ] = True

        state[
            "current_stage"
        ] = "AUDIO_COMPLETE"

        save(
            work / "state.json",
            drive,
            state
        )

        # ====================================================
        # STEP 8
        # COVER + MUSIC
        # ====================================================

        cover = (
            work
            / "assets"
            / "cover.png"
        )

        cover_remote = (
            CONFIG[
                "input_assets"
            ]["drive_path"]
            + "/"
            + CONFIG[
                "input_assets"
            ]["covers_folder"]
            + "/"
            + book_id
            + ".png"
        )

        cover_exists = ensure_local(
            drive,
            cover_remote,
            cover,
            lambda p:
                valid_file(
                    p,
                    256
                )
        )

        music_file = find_music(
            drive,
            work
        )

        if (
            CONFIG.get(
                "require_music",
                False
            )
            and not music_file
        ):

            raise FileNotFoundError(
                "Background music is required "
                "but no music file was found."
            )

        # ====================================================
        # STEP 9
        # VIDEO
        # ====================================================

        video = (
            work
            / "video"
            / "final.mp4"
        )

        remote_video = (
            f"WORK/{book_id}/video/final.mp4"
        )

        if not ensure_local(
            drive,
            remote_video,
            video,
            lambda p:
                valid_media(
                    p,
                    "video"
                )
        ):

            plan = (
                work
                / "video"
                / "source_plan.json"
            )

            remote_plan = (
                f"WORK/{book_id}/video/"
                "source_plan.json"
            )

            if not ensure_local(
                drive,
                remote_plan,
                plan,
                valid_file
            ):

                video_plan = choose_video_plan(
                    drive,
                    duration(audio),
                    work
                )

                plan.write_text(
                    json.dumps(
                        video_plan,
                        indent=2
                    ),
                    encoding="utf-8"
                )

                drive.put_file(
                    plan,
                    remote_plan
                )

            else:

                video_plan = json.loads(
                    plan.read_text(
                        encoding="utf-8"
                    )
                )

            clips = download_plan_clips(
                drive,
                video_plan,
                work
            )

            background = (
                work
                / "video"
                / "background_sequence.mp4"
            )

            remote_background = (
                f"WORK/{book_id}/video/"
                "background_sequence.mp4"
            )

            if not ensure_local(
                drive,
                remote_background,
                background,
                lambda p:
                    valid_media(
                        p,
                        "video"
                    )
            ):

                assemble_video_clips(
                    clips,
                    background,
                    duration(audio),
                    CONFIG
                )

                if not valid_media(
                    background,
                    "video"
                ):

                    raise RuntimeError(
                        "Background video validation failed."
                    )

                drive.put_file(
                    background,
                    remote_background
                )

            build_video(
                background,
                audio,
                video,
                CONFIG,
                cover
                if cover_exists
                else None,
                None,
                music_file
            )

            if not valid_media(
                video,
                "video"
            ):

                raise RuntimeError(
                    "Final video validation failed."
                )

            drive.put_file(
                video,
                remote_video
            )

        state[
            "video_completed"
        ] = True

        state[
            "current_stage"
        ] = "VIDEO_COMPLETE"

        save(
            work / "state.json",
            drive,
            state
        )

        # ====================================================
        # STEP 10
        # THUMBNAIL
        # ====================================================

        thumbnail = (
            work
            / "thumbnail"
            / "final.png"
        )

        remote_thumbnail = (
            f"WORK/{book_id}/thumbnail/"
            "final.png"
        )

        if not ensure_local(
            drive,
            remote_thumbnail,
            thumbnail,
            valid_file
        ):

            template = (
                ROOT
                / "assets"
                / "thumbnail.png"
            )

            if not template.exists():

                raise FileNotFoundError(
                    "assets/thumbnail.png is missing."
                )

            hook = gem.text(
                "metadata",
                f"""
Create exactly ONE powerful YouTube thumbnail hook
for this audiobook topic:

{topic}

Rules:

- Maximum 5 words.
- Strong curiosity.
- Human and natural.
- No fake statistics.
- No sensational false claims.
- Do not simply repeat the title.
- Return ONLY the hook.
"""
            ).strip()

            make(
                template,
                cover
                if cover_exists
                else None,
                hook,
                thumbnail,
                CONFIG[
                    "thumbnail"
                ]
            )

            drive.put_file(
                thumbnail,
                remote_thumbnail
            )

        state[
            "thumbnail_completed"
        ] = True

        save(
            work / "state.json",
            drive,
            state
        )

        # ====================================================
        # STEP 11
        # YOUTUBE METADATA
        # ====================================================

        metadata = (
            work
            / "metadata.json"
        )

        remote_metadata = (
            f"WORK/{book_id}/metadata.json"
        )

        if not ensure_local(
            drive,
            remote_metadata,
            metadata,
            valid_file
        ):

            metadata_prompt = f"""
Create YouTube metadata for an original educational
audiobook.

TOPIC:
{topic}

BOOK TITLE:
{outline.get("title", topic)}

Create:

- title
- description
- hashtags
- tags

Requirements:

1. Accurate.
2. Relevant.
3. Professional.
4. Non-deceptive.
5. Do not claim medical, scientific or financial certainty
   without evidence.
6. Do not invent statistics.
7. Do not invent sources.
8. Do not use misleading clickbait.

Return JSON ONLY:

{{
  "title": "...",
  "description": "...",
  "hashtags": ["..."],
  "tags": ["..."]
}}
"""

            metadata_obj = gem.json(
                "metadata",
                metadata_prompt
            )

            description = (
                metadata_obj.get(
                    "description"
                )
                or ""
            ).strip()

            description += (
                f"\n\n<!-- AI-AUDIOBOOK-ID:{book_id} -->"
            )

            metadata_obj[
                "description"
            ] = description

            metadata.write_text(
                json.dumps(
                    metadata_obj,
                    indent=2,
                    ensure_ascii=False
                ),
                encoding="utf-8"
            )

            drive.put_file(
                metadata,
                remote_metadata
            )

        state[
            "metadata_completed"
        ] = True

        save(
            work / "state.json",
            drive,
            state
        )

        # ====================================================
        # STEP 12
        # METADATA QC
        # ====================================================

        metadata_obj = json.loads(
            metadata.read_text(
                encoding="utf-8"
            )
        )

        if not metadata_obj.get(
            "title"
        ):

            raise RuntimeError(
                "YouTube metadata title is missing."
            )

        if not metadata_obj.get(
            "description"
        ):

            raise RuntimeError(
                "YouTube metadata description is missing."
            )

        if not isinstance(
            metadata_obj.get(
                "hashtags"
            ),
            list
        ):

            raise RuntimeError(
                "YouTube hashtags must be a list."
            )

        if not isinstance(
            metadata_obj.get(
                "tags"
            ),
            list
        ):

            raise RuntimeError(
                "YouTube tags must be a list."
            )

        # ====================================================
        # STEP 13
        # YOUTUBE UPLOAD
        # ====================================================

        existing_id = (
            yt.find_existing_by_marker(
                book_id
            )
        )

        if existing_id:

            log.info(
                "Existing YouTube upload found: %s",
                existing_id
            )

            yt.set_thumbnail(
                existing_id,
                thumbnail
            )

            state[
                "youtube_video_id"
            ] = existing_id

            state[
                "youtube_uploaded"
            ] = True

            if not state.get(
                "youtube_uploaded_at"
            ):

                state[
                    "youtube_uploaded_at"
                ] = datetime.now(
                    timezone.utc
                ).isoformat()

        elif not state.get(
            "youtube_uploaded"
        ):

            state[
                "youtube_upload_intent"
            ] = book_id

            save(
                work / "state.json",
                drive,
                state
            )

            video_id = yt.upload(
                video,
                metadata_obj[
                    "title"
                ],
                metadata_obj[
                    "description"
                ],
                metadata_obj.get(
                    "tags",
                    []
                ),
                CONFIG[
                    "youtube_privacy"
                ],
                thumbnail
            )

            state[
                "youtube_video_id"
            ] = video_id

            state[
                "youtube_uploaded"
            ] = True

            state[
                "youtube_uploaded_at"
            ] = datetime.now(
                timezone.utc
            ).isoformat()

        # ====================================================
        # FINAL SUCCESS
        # ====================================================

        state[
            "status"
        ] = "SUCCESS"

        state[
            "current_stage"
        ] = "SUCCESS"

        state[
            "error"
        ] = None

        state[
            "error_class"
        ] = None

        save(
            work / "state.json",
            drive,
            state
        )

        drive.put_file(
            metadata,
            f"SUCCESS/{book_id}/metadata.json"
        )

        drive.put_file(
            work / "state.json",
            f"SUCCESS/{book_id}/state.json"
        )

        youtube_record = (
            work
            / "youtube.json"
        )

        youtube_record.write_text(
            json.dumps(
                {
                    "book_id": book_id,
                    "video_id": state[
                        "youtube_video_id"
                    ],
                    "uploaded_at": state[
                        "youtube_uploaded_at"
                    ]
                },
                indent=2
            ),
            encoding="utf-8"
        )

        drive.put_file(
            youtube_record,
            f"SUCCESS/{book_id}/youtube.json"
        )

        # ----------------------------------------------------
        # CLEANUP
        # ----------------------------------------------------

        try:

            if not CONFIG.get(
                "archive_success_files",
                False
            ):

                drive.delete_tree(
                    f"WORK/{book_id}"
                )

            else:

                cleanup_local_large(
                    work
                )

        except Exception as cleanup_exc:

            log.warning(
                "Post-publication cleanup failed: %s",
                cleanup_exc
            )

        log.info(
            "BOOK %s completed successfully: %s",
            book_id,
            state[
                "youtube_video_id"
            ]
        )

    # ========================================================
    # FAILURE HANDLING
    # ========================================================

    except Exception as exc:

        error_class = classify_error(
            exc
        )

        state[
            "error"
        ] = str(exc)

        state[
            "error_class"
        ] = error_class

        if error_class == "QUOTA_EXCEEDED":

            state[
                "status"
            ] = "WAITING_FOR_QUOTA"

        else:

            state[
                "status"
            ] = "FAILED"

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

        except Exception as save_exc:

            log.error(
                "Could not persist failure diagnostics: %s",
                save_exc
            )

        raise

    finally:

        heartbeat.stop()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
