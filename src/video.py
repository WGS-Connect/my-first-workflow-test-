from __future__ import annotations
import subprocess
from pathlib import Path

def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def duration(p):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(p)],
        check=True, text=True, stdout=subprocess.PIPE
    )
    return float(r.stdout.strip())

def valid_media(p, kind="format"):
    try:
        args = ["ffprobe", "-v", "error"]
        if kind == "video":
            args += ["-select_streams", "v:0",
                     "-show_entries", "stream=codec_name,width,height,r_frame_rate"]
        elif kind == "audio":
            args += ["-select_streams", "a:0",
                     "-show_entries", "stream=codec_name,sample_rate"]
        else:
            args += ["-show_entries", "format=duration"]
        args += ["-of", "default=nw=1", str(p)]
        subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return Path(p).stat().st_size > 0
    except Exception:
        return False

def assemble_audio(chunks, out):
    lst = out.parent / "concat.txt"
    lst.write_text(
        "\n".join("file '" + str(Path(p).resolve()).replace("'", "'\\''") + "'" for p in chunks),
        encoding="utf-8"
    )
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c:a", "pcm_s16le", str(out)])
    lst.unlink(missing_ok=True)

def assemble_video_clips(clips, out, target_duration, config):
    lst = out.parent / "video_concat.txt"
    lst.write_text(
        "\n".join("file '" + str(Path(p).resolve()).replace("'", "'\\''") + "'" for p in clips),
        encoding="utf-8"
    )
    w, h = config["video_resolution"].split("x")
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={config['video_fps']}"
    )
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-an", "-t", str(target_duration), "-vf", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(out)])
    lst.unlink(missing_ok=True)

def build_video(bg, audio, out, config, cover=None, pips=None, music=None):
    fps = str(config["video_fps"])
    narration_vol = float(config["narration_volume"])
    music_vol = float(config.get("music_volume", 0.08))
    use_cover = bool(cover and config.get("cover", {}).get("enabled"))
    use_music = bool(music)

    if use_music and use_cover:
        inputs = ["-stream_loop", "-1", "-i", str(music), "-i", str(bg), "-i", str(audio), "-i", str(cover)]
        c = config["cover"]
        fc = [
            "[1:v]setpts=PTS-STARTPTS[v0]",
            f"[3:v]scale={c['width']}:{c['height']}:force_original_aspect_ratio=decrease[cv]",
            f"[v0][cv]overlay={c['x']}:{c['y']}[vout0]",
            f"[2:a]volume={narration_vol}[narr]",
            f"[0:a]volume={music_vol}[music]",
            "[narr][music]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            "[vout0]format=yuv420p[vout]"
        ]
    elif use_music:
        inputs = ["-stream_loop", "-1", "-i", str(music), "-i", str(bg), "-i", str(audio)]
        fc = [
            "[1:v]setpts=PTS-STARTPTS,format=yuv420p[vout]",
            f"[2:a]volume={narration_vol}[narr]",
            f"[0:a]volume={music_vol}[music]",
            "[narr][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        ]
    elif use_cover:
        inputs = ["-i", str(bg), "-i", str(audio), "-i", str(cover)]
        c = config["cover"]
        fc = [
            "[0:v]setpts=PTS-STARTPTS[v0]",
            f"[2:v]scale={c['width']}:{c['height']}:force_original_aspect_ratio=decrease[cv]",
            f"[v0][cv]overlay={c['x']}:{c['y']}[vout0]",
            f"[1:a]volume={narration_vol}[aout]",
            "[vout0]format=yuv420p[vout]"
        ]
    else:
        inputs = ["-i", str(bg), "-i", str(audio)]
        fc = [
            "[0:v]setpts=PTS-STARTPTS,format=yuv420p[vout]",
            f"[1:a]volume={narration_vol}[aout]"
        ]

    run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(fc),
        "-map", "[vout]", "-map", "[aout]",
        "-t", str(duration(audio)), "-r", fps,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)
    ])
