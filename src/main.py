from __future__ import annotations
import json, logging, random, re, shutil, threading, time
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.errors import HttpError

from .config import ROOT, CONFIG, VOICE, secret_config
from .drive import Drive
from .state import default, save, load, valid_file
from .gemini import Gemini
from .tts import TTS
from .video import assemble_audio, build_video, valid_media, duration, assemble_video_clips
from .thumbnail import make
from .youtube import YouTube

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("audiobook")


def classify_error(exc):
    status = getattr(exc, "status_code", None)
    if status is None and isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
    text = str(exc).lower()
    if status == 401 or any(x in text for x in ("invalid api key", "authentication", "unauthorized")):
        return "AUTH_ERROR"
    if status == 400 or "invalid request" in text or "invalid argument" in text:
        return "INVALID_REQUEST"
    if status == 404 or "not found" in text:
        return "MISSING_FILE"
    if status in (429, 500, 502, 503, 504) or any(
        x in text for x in ("rate limit", "quota exceeded", "dailylimit", "temporarily unavailable", "timeout")
    ):
        if "quota" in text or "dailylimit" in text:
            return "QUOTA_EXCEEDED"
        return "TEMPORARY"
    if "corrupt" in text or "invalid data" in text:
        return "CORRUPT_FILE"
    return "PERMANENT" if isinstance(exc, (FileNotFoundError, ValueError)) else "TEMPORARY"


class Retry:
    def __init__(self, n, lo, hi):
        self.n, self.lo, self.hi = int(n), float(lo), float(hi)

    def __call__(self, fn, provider, model=None):
        last = None
        for i in range(1, self.n + 1):
            try:
                log.info("PROVIDER %s MODEL %s ATTEMPT %s/%s", provider, model or "-", i, self.n)
                return fn()
            except Exception as exc:
                last = exc
                cls = classify_error(exc)
                if cls in {"AUTH_ERROR", "INVALID_REQUEST", "MISSING_FILE", "CORRUPT_FILE"}:
                    raise
                if i == self.n:
                    raise
                delay = random.uniform(self.lo, self.hi)
                log.warning("%s failed: %s; retrying in %.1fs", provider, exc, delay)
                time.sleep(delay)
        raise last


class Heartbeat:
    def __init__(self, local_state, drive, st, interval=15):
        self.local_state, self.drive, self.st = local_state, drive, st
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _run(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.st["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
                save(self.local_state, self.drive, self.st)
            except Exception as exc:
                log.warning("Heartbeat checkpoint failed: %s", exc)


def topics():
    p = ROOT / "input/topics.txt"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if "|" in line:
            i, t = line.split("|", 1)
            i, t = i.strip(), t.strip()
            if i and t:
                out.append((i, t))
    return sorted(out, key=lambda x: int(x[0]) if x[0].isdigit() else x[0])


def local(book):
    return ROOT / "runtime" / book


def prompts(topic, minutes, outline=None, chapter=None, previous=""):
    if outline is None:
        return f"""Create a rigorous audiobook outline for the topic "{topic}". Target total narration {minutes} minutes.
Return JSON only with keys audience, central_transformation, concept, chapters.
chapters must be an array of objects with number,title,objective,approx_minutes.
Create a coherent educational progression. Do not invent statistics or quotations."""
    if chapter == 0:
        return f"""Write the introduction for an original educational audiobook about "{topic}".
Use a strong hook, relatable tension and curiosity without fabricated statistics, quotations or factual claims.
Natural spoken English. No headings, notes or meta commentary. Target 1200-1800 words."""
    return f"""Write chapter {chapter['number']} titled "{chapter['title']}" for an original educational audiobook about "{topic}".
Objective: {chapter['objective']}. Approximate length: {chapter['approx_minutes']} minutes.
The prior narration is supplied below. Continue its ideas naturally, do not repeat completed material,
and preserve terminology and conceptual continuity. Do not refer to "the previous chapter".
No fabricated statistics, quotations or citations. No meta commentary. Return only narration text.

PRIOR NARRATION:
{previous[-24000:]}
"""


def ensure_local(drive, remote, path, validator):
    if validator(path):
        return True
    try:
        drive.download_file_by_remote(remote, path)
        return validator(path)
    except FileNotFoundError:
        return False


def disk_guard():
    usage = shutil.disk_usage(ROOT)
    free_gb = usage.free / (1024 ** 3)
    minimum = float(CONFIG.get("disk_free_min_gb", 3))
    if free_gb < minimum:
        raise RuntimeError(f"Insufficient disk space: {free_gb:.2f} GB free; minimum {minimum:.2f} GB")


def find_music(drive, work):
    music_dir = work / "assets"
    music_dir.mkdir(parents=True, exist_ok=True)
    remote_root = CONFIG["input_assets"]["drive_path"] + "/" + CONFIG["input_assets"]["music_folder"]
    files = [x for x in drive.list_folder(remote_root) if x.get("mimeType", "").startswith("audio/")]
    if not files:
        return None
    chosen = files[0]
    path = music_dir / chosen["name"]
    if not valid_file(path, 1024):
        drive.download_file(chosen["id"], path)
    return path


def choose_video_plan(drive, audio_duration, work):
    cfg = CONFIG["video_library"]
    root = cfg["drive_path"]
    folders = [x for x in drive.list_folder(root)
               if x.get("mimeType") == "application/vnd.google-apps.folder"]
    if not folders:
        raise FileNotFoundError("No video folders found in Drive VIDEO_LIBRARY")
    folders = sorted(folders, key=lambda x: x["name"].lower())
    folder = random.choice(folders) if cfg.get("random_folder", True) else folders[0]

    files = [x for x in drive.list_folder(root + "/" + folder["name"])
             if x.get("mimeType", "").startswith("video/")]
    if not files:
        raise FileNotFoundError(f"No video clips found in Drive folder {folder['name']}")
    if cfg.get("random_clip_order", True):
        random.shuffle(files)

    chosen, total = [], 0.0
    library_dir = work / "video/library"
    library_dir.mkdir(parents=True, exist_ok=True)
    durations = {}

    for item in files:
        disk_guard()
        local_clip = library_dir / item["name"]
        if not valid_media(local_clip, "video"):
            drive.download_file(item["id"], local_clip)
        d = duration(local_clip)
        durations[item["id"]] = d
        chosen.append({"id": item["id"], "name": item["name"]})
        total += d
        if total >= audio_duration:
            break

    if total < audio_duration:
        if not cfg.get("allow_clip_duplication", True):
            raise RuntimeError(
                f"Video library duration is insufficient ({total:.1f}s < {audio_duration:.1f}s) "
                "and allow_clip_duplication=false."
            )
        if not chosen:
            raise RuntimeError("No usable video clips available for duplication.")
        while total < audio_duration:
            item = random.choice(chosen)
            chosen.append(item)
            total += durations[item["id"]]

    if cfg.get("random_clip_order", True):
        random.shuffle(chosen)

    return {"folder_id": folder["id"], "folder": folder["name"], "clips": chosen}


def download_plan_clips(drive, plan, work):
    library_dir = work / "video/library"
    library_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for item in plan["clips"]:
        p = library_dir / item["name"]
        if not valid_media(p, "video"):
            drive.download_file(item["id"], p)
        if not valid_media(p, "video"):
            raise RuntimeError(f"Video clip validation failed: {item['name']}")
        paths.append(p)
    return paths


def cleanup_local_large(work):
    for name in ("video", "audio"):
        p = work / name
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)


def main():
    sec = secret_config()
    retry = Retry(CONFIG["retry_count"], CONFIG["retry_delay_min"], CONFIG["retry_delay_max"])
    drive = Drive(sec["google_drive_credentials"], retry)
    gem = Gemini(sec["gemini_api_key"], retry, CONFIG, VOICE)
    tts = TTS(retry, VOICE)
    yt = YouTube(sec["youtube_client_id"], sec["youtube_client_secret"], sec["youtube_refresh_token"], retry)

    for folder in ("STATE", "WORK", "SUCCESS", "FAILED"):
        drive.folder_path(folder)

    selected = None
    for bid, topic in topics():
        p = local(bid)
        p.mkdir(parents=True, exist_ok=True)
        s = load(p / "state.json", drive, bid)
        if s and s.get("status") == "SUCCESS":
            continue
        selected = (bid, topic, s, p)
        break

    if not selected:
        log.info("No eligible book")
        return

    bid, topic, st, work = selected
    st = st or default(bid, topic)
    st["topic"] = topic
    st["status"] = "RUNNING"
    save(work / "state.json", drive, st)
    hb = Heartbeat(work / "state.json", drive, st)
    hb.start()

    try:
        disk_guard()
        for d in ("chapters", "audio", "video", "thumbnail", "script"):
            (work / d).mkdir(parents=True, exist_ok=True)

        outline_path = work / "outline.json"
        if not ensure_local(drive, f"WORK/{bid}/outline.json", outline_path, valid_file):
            outline = gem.json("script", prompts(topic, (CONFIG["script_minutes_min"] + CONFIG["script_minutes_max"]) // 2))
            outline_path.write_text(json.dumps(outline, indent=2, ensure_ascii=False), encoding="utf-8")
            drive.put_file(outline_path, f"WORK/{bid}/outline.json")
        else:
            outline = json.loads(outline_path.read_text(encoding="utf-8"))
        st["current_stage"] = "OUTLINE_COMPLETE"
        save(work / "state.json", drive, st)

        intro = work / "chapters/00_intro.txt"
        if not ensure_local(drive, f"WORK/{bid}/chapters/00_intro.txt", intro, valid_file):
            intro.write_text(gem.text("script", prompts(topic, 0, outline, 0)), encoding="utf-8")
            drive.put_file(intro, f"WORK/{bid}/chapters/00_intro.txt")
        st["current_stage"] = "INTRO_COMPLETE"
        save(work / "state.json", drive, st)

        chapters = outline.get("chapters", [])
        if not chapters:
            raise ValueError("Outline contains no chapters")
        st["chapter_count"] = len(chapters)
        save(work / "state.json", drive, st)

        for ch in chapters:
            n = int(ch["number"])
            f = work / f"chapters/{n:02d}.txt"
            remote = f"WORK/{bid}/chapters/{n:02d}.txt"
            if ensure_local(drive, remote, f, valid_file):
                st["last_completed_chapter"] = max(st["last_completed_chapter"], n)
                continue
            previous_parts = [intro]
            for prev in chapters:
                if int(prev["number"]) >= n:
                    break
                previous_parts.append(work / f"chapters/{int(prev['number']):02d}.txt")
            previous = "\n\n".join(x.read_text(encoding="utf-8") for x in previous_parts if x.exists())
            f.write_text(gem.text("script", prompts(topic, 0, outline, ch, previous)), encoding="utf-8")
            if not valid_file(f):
                raise RuntimeError(f"Generated chapter {n} failed validation")
            drive.put_file(f, remote)
            st["last_completed_chapter"] = n
            st["current_stage"] = f"CHAPTER_{n:02d}_COMPLETE"
            save(work / "state.json", drive, st)

        final = work / "script/final_script.txt"
        remote_final = f"WORK/{bid}/script/final_script.txt"
        if not ensure_local(drive, remote_final, final, valid_file):
            ordered = [intro] + [work / f"chapters/{int(c['number']):02d}.txt" for c in chapters]
            txt = "\n\n".join(x.read_text(encoding="utf-8").strip() for x in ordered)
            final.write_text(txt, encoding="utf-8")
            drive.put_file(final, remote_final)

        script_text = final.read_text(encoding="utf-8")
        wc = len(script_text.split())
        min_words = CONFIG["script_minutes_min"] * 120
        max_words = CONFIG["script_minutes_max"] * 180
        if wc < min_words or wc > max_words:
            raise RuntimeError(f"Script length QC failed: {wc} words; expected {min_words}-{max_words}")
        if re.search(r"\b(TODO|TBD|PLACEHOLDER|INSERT .* HERE)\b", script_text, re.I):
            raise RuntimeError("Script quality control found unfinished placeholder text")
        st["current_stage"] = "SCRIPT_COMPLETE"
        save(work / "state.json", drive, st)

        words = script_text.split()
        chunk_words = 900
        chunks = [" ".join(words[i:i + chunk_words]) for i in range(0, len(words), chunk_words)]
        st["total_chunks"] = len(chunks)
        save(work / "state.json", drive, st)

        provider = st.get("tts_provider")
        for i, text in enumerate(chunks, 1):
            disk_guard()
            out = work / f"audio/{i:04d}.wav"
            remote = f"WORK/{bid}/audio/{i:04d}.wav"
            if ensure_local(drive, remote, out, lambda p: valid_media(p, "audio")):
                st["completed_chunks"] = sorted(set(st["completed_chunks"] + [i]))
                continue
            used = tts.make(text, out, provider if CONFIG["allow_provider_switch_within_book"] else None)
            provider = used
            st["tts_provider"] = provider
            st["completed_chunks"] = sorted(set(st["completed_chunks"] + [i]))
            drive.put_file(out, remote)
            save(work / "state.json", drive, st)

        audio = work / "audio/final_audio.wav"
        remote_audio = f"WORK/{bid}/audio/final_audio.wav"
        if not ensure_local(drive, remote_audio, audio, lambda p: valid_media(p, "audio")):
            audio_chunks = [work / f"audio/{i:04d}.wav" for i in range(1, len(chunks) + 1)]
            for p in audio_chunks:
                if not valid_media(p, "audio"):
                    raise FileNotFoundError(f"Missing audio chunk required for assembly: {p.name}")
            assemble_audio(audio_chunks, audio)
            if not valid_media(audio, "audio"):
                raise RuntimeError("Final audio validation failed")
            drive.put_file(audio, remote_audio)
        st["audio_completed"] = True
        st["current_stage"] = "AUDIO_COMPLETE"
        save(work / "state.json", drive, st)

        cover = work / "assets/cover.png"
        cover_remote = f"{CONFIG['input_assets']['drive_path']}/{CONFIG['input_assets']['covers_folder']}/{bid}.png"
        cover_exists = ensure_local(drive, cover_remote, cover, lambda p: valid_file(p, 256))
        music_file = find_music(drive, work)
        if CONFIG.get("require_music") and not music_file:
            raise FileNotFoundError("Configured background music is required but no Drive music file exists")

        video = work / "video/final.mp4"
        remote_video = f"WORK/{bid}/video/final.mp4"
        if not ensure_local(drive, remote_video, video, lambda p: valid_media(p, "video")):
            plan = work / "video/source_plan.json"
            remote_plan = f"WORK/{bid}/video/source_plan.json"
            if not ensure_local(drive, remote_plan, plan, valid_file):
                vp = choose_video_plan(drive, duration(audio), work)
                plan.write_text(json.dumps(vp, indent=2), encoding="utf-8")
                drive.put_file(plan, remote_plan)
            else:
                vp = json.loads(plan.read_text(encoding="utf-8"))
            clips = download_plan_clips(drive, vp, work)
            bg = work / "video/background_sequence.mp4"
            remote_bg = f"WORK/{bid}/video/background_sequence.mp4"
            if not ensure_local(drive, remote_bg, bg, lambda p: valid_media(p, "video")):
                assemble_video_clips(clips, bg, duration(audio), CONFIG)
                if not valid_media(bg, "video"):
                    raise RuntimeError("Background video validation failed")
                drive.put_file(bg, remote_bg)
            build_video(bg, audio, video, CONFIG, cover if cover_exists else None, None, music_file)
            if not valid_media(video, "video"):
                raise RuntimeError("Final video validation failed")
            drive.put_file(video, remote_video)

        st["video_completed"] = True
        st["current_stage"] = "VIDEO_COMPLETE"
        save(work / "state.json", drive, st)

        thumb = work / "thumbnail/final.png"
        remote_thumb = f"WORK/{bid}/thumbnail/final.png"
        if not ensure_local(drive, remote_thumb, thumb, valid_file):
            template = ROOT / "assets/thumbnail.png"
            if not template.exists():
                raise FileNotFoundError("assets/thumbnail.png is missing")
            hook = gem.text("metadata", f'Give exactly one short YouTube thumbnail hook for "{topic}". Maximum 5 words. No title repetition. Return only the hook.')
            make(template, cover if cover_exists else None, hook, thumb, CONFIG["thumbnail"])
            drive.put_file(thumb, remote_thumb)
        st["thumbnail_completed"] = True
        save(work / "state.json", drive, st)

        meta = work / "metadata.json"
        remote_meta = f"WORK/{bid}/metadata.json"
        if not ensure_local(drive, remote_meta, meta, valid_file):
            m = gem.json("metadata", f'''Create YouTube metadata for an original audiobook about "{topic}".
Return JSON only with title,description,hashtags,tags. Accurate, relevant, non-deceptive.
hashtags and tags arrays.''')
            m["description"] = (m.get("description") or "").strip() + f"\n\n<!-- AI-AUDIOBOOK-ID:{bid} -->"
            meta.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
            drive.put_file(meta, remote_meta)
        st["metadata_completed"] = True
        save(work / "state.json", drive, st)

        m = json.loads(meta.read_text(encoding="utf-8"))
        if not m.get("title") or not m.get("description") or not isinstance(m.get("hashtags"), list):
            raise RuntimeError("Metadata quality control failed")

        existing_id = yt.find_existing_by_marker(bid)
        if existing_id:
            # The video may have been accepted immediately before a runner crash.
            # Re-apply the thumbnail safely before marking the publication complete.
            yt.set_thumbnail(existing_id, thumb)
            st["youtube_video_id"] = existing_id
            st["youtube_uploaded"] = True
            st["youtube_uploaded_at"] = st.get("youtube_uploaded_at") or datetime.now(timezone.utc).isoformat()
        elif not st.get("youtube_uploaded"):
            st["youtube_upload_intent"] = bid
            save(work / "state.json", drive, st)
            vid = yt.upload(video, m["title"], m["description"], m.get("tags", []), CONFIG["youtube_privacy"], thumb)
            st["youtube_video_id"] = vid
            st["youtube_uploaded"] = True
            st["youtube_uploaded_at"] = datetime.now(timezone.utc).isoformat()

        st["status"] = "SUCCESS"
        st["current_stage"] = "SUCCESS"
        st["error"] = None
        st["error_class"] = None
        save(work / "state.json", drive, st)

        drive.put_file(meta, f"SUCCESS/{bid}/metadata.json")
        drive.put_file(work / "state.json", f"SUCCESS/{bid}/state.json")
        youtube_record = work / "youtube.json"
        youtube_record.write_text(json.dumps({"book_id": bid, "video_id": st["youtube_video_id"], "uploaded_at": st["youtube_uploaded_at"]}, indent=2), encoding="utf-8")
        drive.put_file(youtube_record, f"SUCCESS/{bid}/youtube.json")
        try:
            if not CONFIG.get("archive_success_files"):
                drive.delete_tree(f"WORK/{bid}")
            else:
                cleanup_local_large(work)
        except Exception as cleanup_exc:
            # Publication is already confirmed; cleanup failure must not turn SUCCESS back into FAILED.
            log.warning("Post-publication cleanup failed for %s: %s", bid, cleanup_exc)
        log.info("BOOK %s completed successfully: %s", bid, st["youtube_video_id"])

    except Exception as exc:
        cls = classify_error(exc)
        st["error"] = str(exc)
        st["error_class"] = cls
        st["status"] = "WAITING_FOR_QUOTA" if cls == "QUOTA_EXCEEDED" else "FAILED"
        save(work / "state.json", drive, st)
        (work / "error.log").write_text(f"{cls}: {exc}\n", encoding="utf-8")
        try:
            drive.put_file(work / "state.json", f"FAILED/{bid}/state.json")
            drive.put_file(work / "error.log", f"FAILED/{bid}/error.log")
        except Exception as save_exc:
            log.error("Could not persist failure diagnostics: %s", save_exc)
        raise
    finally:
        hb.stop()


if __name__ == "__main__":
    main()
