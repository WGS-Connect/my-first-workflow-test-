from __future__ import annotations

import logging
import subprocess
from pathlib import Path


log = logging.getLogger("audiobook.video")


# ============================================================
# BASIC FFMPEG RUNNER
# ============================================================

def run(
    cmd,
    timeout=None
):

    log.debug(
        "Running FFmpeg command: %s",
        " ".join(
            str(x)
            for x in cmd
        )
    )

    try:

        result = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )

        return result

    except subprocess.CalledProcessError as exc:

        error = (
            exc.stderr
            or exc.stdout
            or str(exc)
        )

        raise RuntimeError(
            "FFmpeg command failed:\n"
            + error[-6000:]
        ) from exc


# ============================================================
# MEDIA DURATION
# ============================================================

def duration(
    p
):

    path = Path(
        p
    )

    if not path.exists():

        raise FileNotFoundError(
            f"Media file not found: {path}"
        )

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path)
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    value = (
        result.stdout.strip()
    )

    if not value:

        raise RuntimeError(
            f"Unable to determine media duration: {path}"
        )

    seconds = float(
        value
    )

    if seconds <= 0:

        raise RuntimeError(
            f"Invalid media duration: {path}"
        )

    return seconds


# ============================================================
# MEDIA VALIDATION
# ============================================================

def valid_media(
    p,
    kind="format"
):

    path = Path(
        p
    )

    try:

        if (
            not path.exists()
            or not path.is_file()
            or path.stat().st_size <= 0
        ):

            return False


        args = [
            "ffprobe",
            "-v",
            "error"
        ]


        if kind == "video":

            args += [
                "-select_streams",
                "v:0",
                "-show_entries",
                (
                    "stream="
                    "codec_name,"
                    "width,"
                    "height,"
                    "r_frame_rate"
                )
            ]


        elif kind == "audio":

            args += [
                "-select_streams",
                "a:0",
                "-show_entries",
                (
                    "stream="
                    "codec_name,"
                    "sample_rate,"
                    "channels"
                )
            ]


        else:

            args += [
                "-show_entries",
                "format=duration"
            ]


        args += [
            "-of",
            "default=nw=1",
            str(path)
        ]


        result = subprocess.run(
            args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )


        if not result.stdout.strip():

            return False


        if kind == "format":

            try:

                return (
                    float(
                        result.stdout.strip()
                    ) > 0
                )

            except ValueError:

                return False


        return True


    except Exception:

        return False


# ============================================================
# SAFE CONCAT FILE
# ============================================================

def _ffconcat_escape(
    path
):

    value = str(
        Path(path).resolve()
    )

    return (
        value
        .replace(
            "'",
            "'\\''"
        )
    )


def _write_concat_file(
    paths,
    output
):

    output = Path(
        output
    )

    output.parent.mkdir(
        parents=True,
        exist_ok=True
    )


    lines = []

    for path in paths:

        path = Path(
            path
        )

        if not path.exists():

            raise FileNotFoundError(
                f"Concat input does not exist: {path}"
            )

        lines.append(
            "file '"
            + _ffconcat_escape(path)
            + "'"
        )


    if not lines:

        raise RuntimeError(
            "No media files were supplied."
        )


    output.write_text(
        "\n".join(lines)
        + "\n",
        encoding="utf-8"
    )

    return output


# ============================================================
# ASSEMBLE AUDIO CHUNKS
# ============================================================

def assemble_audio(
    chunks,
    out
):

    out = Path(
        out
    )

    out.parent.mkdir(
        parents=True,
        exist_ok=True
    )


    if not chunks:

        raise RuntimeError(
            "No audio chunks were supplied."
        )


    for chunk in chunks:

        if not valid_media(
            chunk,
            "audio"
        ):

            raise RuntimeError(
                f"Invalid audio chunk: {chunk}"
            )


    concat_file = (
        out.parent
        / "concat_audio.txt"
    )

    temporary = (
        out.parent
        / (
            out.stem
            + ".assembling"
            + out.suffix
        )
    )


    try:

        _write_concat_file(
            chunks,
            concat_file
        )


        run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-vn",
                "-c:a",
                "pcm_s16le",
                str(temporary)
            ]
        )


        if not valid_media(
            temporary,
            "audio"
        ):

            raise RuntimeError(
                "FFmpeg created invalid "
                "assembled audio."
            )


        temporary.replace(
            out
        )


        return out


    finally:

        concat_file.unlink(
            missing_ok=True
        )

        temporary.unlink(
            missing_ok=True
        )


# ============================================================
# ASSEMBLE VIDEO CLIPS
# ============================================================

def assemble_video_clips(
    clips,
    out,
    target_duration,
    config
):

    out = Path(
        out
    )

    out.parent.mkdir(
        parents=True,
        exist_ok=True
    )


    if not clips:

        raise RuntimeError(
            "No background video clips were supplied."
        )


    target_duration = float(
        target_duration
    )

    if target_duration <= 0:

        raise ValueError(
            "target_duration must be greater than zero."
        )


    for clip in clips:

        if not valid_media(
            clip,
            "video"
        ):

            raise RuntimeError(
                f"Invalid video clip: {clip}"
            )


    resolution = str(
        config.get(
            "video_resolution",
            "1280x720"
        )
    )

    try:

        width, height = (
            resolution.split("x")
        )

        width = int(
            width
        )

        height = int(
            height
        )

    except Exception as exc:

        raise RuntimeError(
            "video_resolution must be "
            "in WIDTHxHEIGHT format."
        ) from exc


    fps = int(
        config.get(
            "video_fps",
            24
        )
    )


    concat_file = (
        out.parent
        / "video_concat.txt"
    )

    temporary = (
        out.parent
        / (
            out.stem
            + ".assembling"
            + out.suffix
        )
    )


    filter_video = (
        f"scale={width}:{height}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:"
        "(ow-iw)/2:(oh-ih)/2,"
        f"fps={fps},"
        "format=yuv420p"
    )


    try:

        _write_concat_file(
            clips,
            concat_file
        )


        run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-an",
                "-t",
                str(target_duration),
                "-vf",
                filter_video,
                "-r",
                str(fps),
                "-c:v",
                "libx264",
                "-preset",
                str(
                    config.get(
                        "video_preset",
                        "veryfast"
                    )
                ),
                "-crf",
                str(
                    config.get(
                        "video_crf",
                        23
                    )
                ),
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(temporary)
            ]
        )


        if not valid_media(
            temporary,
            "video"
        ):

            raise RuntimeError(
                "FFmpeg created invalid "
                "background video."
            )


        temporary.replace(
            out
        )


        return out


    finally:

        concat_file.unlink(
            missing_ok=True
        )

        temporary.unlink(
            missing_ok=True
        )


# ============================================================
# BUILD FINAL AUDIOBOOK VIDEO
# ============================================================

def build_video(
    bg,
    audio,
    out,
    config,
    cover=None,
    pips=None,
    music=None
):

    bg = Path(
        bg
    )

    audio = Path(
        audio
    )

    out = Path(
        out
    )


    if not valid_media(
        bg,
        "video"
    ):

        raise RuntimeError(
            f"Background video is invalid: {bg}"
        )


    if not valid_media(
        audio,
        "audio"
    ):

        raise RuntimeError(
            f"Narration audio is invalid: {audio}"
        )


    if cover:

        cover = Path(
            cover
        )

        if not valid_media(
            cover,
            "format"
        ):

            raise RuntimeError(
                f"Cover image is invalid: {cover}"
            )


    if music:

        music = Path(
            music
        )

        if not valid_media(
            music,
            "audio"
        ):

            raise RuntimeError(
                f"Music file is invalid: {music}"
            )


    output_duration = duration(
        audio
    )


    if output_duration <= 0:

        raise RuntimeError(
            "Narration duration is zero."
        )


    # ========================================================
    # SETTINGS
    # ========================================================

    fps = int(
        config.get(
            "video_fps",
            24
        )
    )

    narration_volume = float(
        config.get(
            "narration_volume",
            1.0
        )
    )

    music_volume = float(
        config.get(
            "music_volume",
            0.08
        )
    )


    cover_config = config.get(
        "cover",
        {}
    )

    if not isinstance(
        cover_config,
        dict
    ):

        cover_config = {}


    use_cover = bool(
        cover
        and cover_config.get(
            "enabled",
            True
        )
    )

    use_music = bool(
        music
    )


    # ========================================================
    # INPUTS
    # ========================================================

    inputs = []

    filter_parts = []


    # --------------------------------------------------------
    # Background
    # --------------------------------------------------------

    inputs += [
        "-stream_loop",
        "-1",
        "-i",
        str(bg)
    ]

    bg_index = 0


    # --------------------------------------------------------
    # Narration
    # --------------------------------------------------------

    inputs += [
        "-i",
        str(audio)
    ]

    narration_index = 1


    # --------------------------------------------------------
    # Music
    # --------------------------------------------------------

    music_index = None

    if use_music:

        music_index = 2

        inputs += [
            "-stream_loop",
            "-1",
            "-i",
            str(music)
        ]


    # --------------------------------------------------------
    # Cover
    # --------------------------------------------------------

    cover_index = None

    if use_cover:

        cover_index = (
            3
            if use_music
            else 2
        )

        inputs += [
            "-loop",
            "1",
            "-i",
            str(cover)
        ]


    # ========================================================
    # VIDEO FILTER
    # ========================================================

    video_resolution = str(
        config.get(
            "video_resolution",
            "1280x720"
        )
    )

    try:

        width, height = (
            video_resolution.split("x")
        )

        width = int(
            width
        )

        height = int(
            height
        )

    except Exception as exc:

        raise RuntimeError(
            "video_resolution must be "
            "WIDTHxHEIGHT."
        ) from exc


    filter_parts.append(
        (
            f"[{bg_index}:v]"
            "setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:"
            "(ow-iw)/2:(oh-ih)/2,"
            f"fps={fps},"
            "format=yuv420p"
            "[bgv]"
        )
    )


    current_video = (
        "[bgv]"
    )


    # ========================================================
    # COVER OVERLAY
    # ========================================================

    if use_cover:

        cover_width = cover_config.get(
            "width",
            260
        )

        cover_height = cover_config.get(
            "height",
            -1
        )

        cover_x = cover_config.get(
            "x",
            40
        )

        cover_y = cover_config.get(
            "y",
            40
        )


        filter_parts.append(
            (
                f"[{cover_index}:v]"
                f"scale={cover_width}:{cover_height}:"
                "force_original_aspect_ratio=decrease,"
                "format=rgba"
                "[coverv]"
            )
        )


        filter_parts.append(
            (
                f"{current_video}"
                "[coverv]"
                f"overlay={cover_x}:{cover_y}:"
                "shortest=0"
                "[covered]"
            )
        )


        current_video = (
            "[covered]"
        )


    filter_parts.append(
        (
            f"{current_video}"
            "format=yuv420p"
            "[vout]"
        )
    )


    # ========================================================
    # NARRATION AUDIO
    # ========================================================

    filter_parts.append(
        (
            f"[{narration_index}:a]"
            f"volume={narration_volume}"
            "[narr]"
        )
    )


    # ========================================================
    # MUSIC MIX
    # ========================================================

    if use_music:

        filter_parts.append(
            (
                f"[{music_index}:a]"
                f"volume={music_volume},"
                "aresample=async=1"
                "[music]"
            )
        )


        filter_parts.append(
            (
                "[narr]"
                "[music]"
                "amix=inputs=2:"
                "duration=first:"
                "dropout_transition=3:"
                "normalize=0"
                "[aout]"
            )
        )

    else:

        filter_parts.append(
            "[narr][aout]"
            if False
            else
            "[narr]anull[aout]"
        )


    filter_complex = ";".join(
        filter_parts
    )


    # ========================================================
    # ATOMIC OUTPUT
    # ========================================================

    out.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = (
        out.parent
        / (
            out.stem
            + ".rendering"
            + out.suffix
        )
    )


    temporary.unlink(
        missing_ok=True
    )


    # ========================================================
    # FINAL FFMPEG
    # ========================================================

    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",

        *inputs,

        "-filter_complex",
        filter_complex,

        "-map",
        "[vout]",

        "-map",
        "[aout]",

        "-t",
        str(output_duration),

        "-r",
        str(fps),

        "-c:v",
        "libx264",

        "-preset",
        str(
            config.get(
                "video_preset",
                "veryfast"
            )
        ),

        "-crf",
        str(
            config.get(
                "video_crf",
                23
            )
        ),

        "-pix_fmt",
        "yuv420p",

        "-c:a",
        "aac",

        "-b:a",
        str(
            config.get(
                "audio_bitrate",
                "192k"
            )
        ),

        "-ar",
        "48000",

        "-ac",
        "2",

        "-movflags",
        "+faststart",

        str(temporary)
    ]


    try:

        log.info(
            "Rendering final audiobook video: "
            "%.1f seconds",
            output_duration
        )


        run(
            command
        )


        if not valid_media(
            temporary,
            "format"
        ):

            raise RuntimeError(
                "Final video failed validation."
            )


        # ----------------------------------------------------
        # Make sure the final video has both streams.
        # ----------------------------------------------------

        if not valid_media(
            temporary,
            "video"
        ):

            raise RuntimeError(
                "Final video has no valid video stream."
            )


        if not valid_media(
            temporary,
            "audio"
        ):

            raise RuntimeError(
                "Final video has no valid audio stream."
            )


        temporary.replace(
            out
        )


        log.info(
            "Final audiobook video created: %s",
            out
        )


        return out


    finally:

        temporary.unlink(
            missing_ok=True
        )
