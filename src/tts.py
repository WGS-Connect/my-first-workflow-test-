from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path


import edge_tts


log = logging.getLogger("audiobook.tts")


# ============================================================
# EDGE TTS HELPERS
# ============================================================

def _edge_rate(
    speed
):
    """
    Convert normal playback speed into Edge TTS rate.

    1.00 = +0%
    1.10 = +10%
    0.90 = -10%
    """

    value = float(
        speed
    )

    percent = round(
        (value - 1.0) * 100
    )

    return f"{percent:+d}%"


def _edge_pitch(
    pitch
):
    """
    Edge TTS pitch is expressed in Hz.
    """

    return f"{float(pitch):+g}Hz"


# ============================================================
# TTS ENGINE
# ============================================================

class TTS:

    def __init__(
        self,
        retry,
        voice
    ):

        self.retry = retry

        self.v = (
            voice
            if isinstance(
                voice,
                dict
            )
            else {}
        )


    # ========================================================
    # TEMPORARY FILE
    # ========================================================

    @staticmethod
    def _temporary_mp3():

        handle = tempfile.NamedTemporaryFile(
            suffix=".mp3",
            delete=False
        )

        handle.close()

        return Path(
            handle.name
        )


    # ========================================================
    # VALIDATE OUTPUT
    # ========================================================

    @staticmethod
    def _valid_output(
        path
    ):

        path = Path(
            path
        )

        try:

            return (
                path.exists()
                and path.is_file()
                and path.stat().st_size > 1024
            )

        except OSError:

            return False


    # ========================================================
    # EDGE NEURAL TTS
    # ========================================================

    def edge(
        self,
        text,
        out
    ):

        out = Path(
            out
        )

        out.parent.mkdir(
            parents=True,
            exist_ok=True
        )


        async def generate():

            temporary = (
                self._temporary_mp3()
            )

            try:

                speed = float(
                    self.v.get(
                        "speed",
                        1.0
                    )
                )

                pitch = float(
                    self.v.get(
                        "pitch",
                        0
                    )
                )

                edge_config = self.v.get(
                    "edge_tts",
                    {}
                )

                if not isinstance(
                    edge_config,
                    dict
                ):

                    edge_config = {}


                # ------------------------------------------------
                # VOICE
                # ------------------------------------------------

                voice = (
                    edge_config.get(
                        "voice"
                    )
                )

                if not voice:

                    raise RuntimeError(
                        "Edge TTS voice is not configured."
                    )


                # ------------------------------------------------
                # RATE
                # ------------------------------------------------

                rate = (
                    _edge_rate(
                        speed
                    )
                )


                # ------------------------------------------------
                # PITCH
                # ------------------------------------------------

                epitch = (
                    _edge_pitch(
                        pitch
                    )
                )


                log.info(
                    "Generating Edge Neural TTS: "
                    "voice=%s rate=%s pitch=%s",
                    voice,
                    rate,
                    epitch
                )


                communicator = (
                    edge_tts.Communicate(
                        text,
                        voice,
                        rate=rate,
                        pitch=epitch
                    )
                )


                await communicator.save(
                    str(temporary)
                )


                if not self._valid_output(
                    temporary
                ):

                    raise RuntimeError(
                        "Edge TTS produced an empty "
                        "or invalid audio file."
                    )


                # ------------------------------------------------
                # Convert MP3 → WAV
                # ------------------------------------------------

                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(temporary),
                        "-vn",
                        "-c:a",
                        "pcm_s16le",
                        "-ar",
                        "24000",
                        "-ac",
                        "1",
                        str(out)
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )


                if not self._valid_output(
                    out
                ):

                    raise RuntimeError(
                        "FFmpeg produced an invalid "
                        "Edge TTS output."
                    )


            finally:

                temporary.unlink(
                    missing_ok=True
                )


        def run():

            asyncio.run(
                generate()
            )

            return out


        self.retry(
            run,
            "edge_tts",
            model=self.v.get(
                "edge_tts",
                {}
            ).get(
                "voice",
                "unknown"
            )
        )

        return out


    # ========================================================
    # KOKORO FALLBACK
    # ========================================================

    def kokoro(
        self,
        text,
        out
    ):

        out = Path(
            out
        )

        out.parent.mkdir(
            parents=True,
            exist_ok=True
        )


        def run():

            try:

                from kokoro import (
                    KPipeline
                )

                import numpy as np
                import soundfile as sf

            except ImportError as exc:

                raise RuntimeError(
                    "Kokoro is not installed. "
                    "Install the required Kokoro "
                    "dependencies before using "
                    "the fallback."
                ) from exc


            language = str(
                self.v.get(
                    "language",
                    "en-US"
                )
            )

            language_code = (
                language
                .split("-")[0]
            )


            kokoro_config = (
                self.v.get(
                    "kokoro",
                    {}
                )
            )

            if not isinstance(
                kokoro_config,
                dict
            ):

                kokoro_config = {}


            voice = (
                kokoro_config.get(
                    "voice"
                )
            )

            if not voice:

                raise RuntimeError(
                    "Kokoro fallback voice "
                    "is not configured."
                )


            speed = float(
                self.v.get(
                    "speed",
                    kokoro_config.get(
                        "speed",
                        1.0
                    )
                )
            )


            log.info(
                "Generating Kokoro TTS: "
                "voice=%s speed=%s language=%s",
                voice,
                speed,
                language_code
            )


            pipeline = KPipeline(
                lang_code=language_code
            )


            audio_chunks = []


            for (
                _graphemes,
                _phonemes,
                audio
            ) in pipeline(
                text,
                voice=voice,
                speed=speed
            ):

                if audio is None:
                    continue

                audio_chunks.append(
                    np.asarray(
                        audio
                    )
                )


            if not audio_chunks:

                raise RuntimeError(
                    "Kokoro returned no audio."
                )


            combined = np.concatenate(
                audio_chunks
            )


            sf.write(
                str(out),
                combined,
                24000
            )


            if not self._valid_output(
                out
            ):

                raise RuntimeError(
                    "Kokoro produced an invalid "
                    "audio file."
                )


            return out


        self.retry(
            run,
            "kokoro",
            model=(
                self.v.get(
                    "kokoro",
                    {}
                ).get(
                    "voice",
                    "unknown"
                )
            )
        )

        return out


    # ========================================================
    # PROVIDER ORDER
    # ========================================================

    def _provider_order(
        self,
        provider=None
    ):

        order = [
            "edge_tts",
            "kokoro"
        ]


        if provider:

            provider = (
                str(
                    provider
                ).strip().lower()
            )

            if provider in order:

                index = (
                    order.index(
                        provider
                    )
                )

                order = (
                    order[index:]
                )


        return order


    # ========================================================
    # MAIN TTS METHOD
    # ========================================================

    def make(
        self,
        text,
        out,
        provider=None
    ):

        out = Path(
            out
        )

        out.parent.mkdir(
            parents=True,
            exist_ok=True
        )


        if not str(
            text
        ).strip():

            raise ValueError(
                "TTS text is empty."
            )


        order = (
            self._provider_order(
                provider
            )
        )


        last_error = None


        for current_provider in order:

            # ------------------------------------------------
            # Remove stale output before attempting a provider.
            # ------------------------------------------------

            try:

                out.unlink(
                    missing_ok=True
                )

            except OSError:
                pass


            try:

                log.info(
                    "Trying TTS provider: %s",
                    current_provider
                )


                if current_provider == "edge_tts":

                    self.edge(
                        text,
                        out
                    )

                elif current_provider == "kokoro":

                    self.kokoro(
                        text,
                        out
                    )

                else:

                    raise RuntimeError(
                        f"Unsupported TTS provider: "
                        f"{current_provider}"
                    )


                if not self._valid_output(
                    out
                ):

                    raise RuntimeError(
                        f"{current_provider} "
                        "returned invalid audio."
                    )


                log.info(
                    "TTS successful using %s",
                    current_provider
                )


                return current_provider


            except Exception as exc:

                last_error = exc

                log.warning(
                    "TTS provider %s failed: %s",
                    current_provider,
                    exc
                )


                try:

                    out.unlink(
                        missing_ok=True
                    )

                except OSError:
                    pass


                # Continue automatically to the
                # next configured provider.


        if last_error:

            raise RuntimeError(
                "All TTS providers failed. "
                f"Last error: {last_error}"
            ) from last_error


        raise RuntimeError(
            "No TTS providers are configured."
        )
