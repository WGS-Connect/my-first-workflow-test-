from __future__ import annotations

import json
import re
from typing import Any, Dict

from google import genai


class Gemini:
    """
    Gemini client used by the audiobook pipeline.

    Responsibilities:
    - Send text prompts to Gemini.
    - Support model fallback configured in config.json.
    - Return plain text.
    - Parse Gemini JSON responses safely.
    - Leave retry/backoff behavior to the existing Retry object.
    """

    def __init__(
        self,
        key: str,
        retry,
        config: Dict[str, Any],
        voice: Dict[str, Any] | None = None,
    ):
        if not key:
            raise ValueError("Gemini API key is missing.")

        self.client = genai.Client(
            api_key=key
        )

        self.retry = retry
        self.config = config
        self.voice = voice or {}

    # ---------------------------------------------------------
    # MODEL SELECTION
    # ---------------------------------------------------------

    def _models_for(self, purpose: str) -> list[str]:

        models = self.config.get(
            "models",
            {}
        )

        selected = models.get(
            purpose
        )

        if not selected:
            selected = models.get(
                "script"
            )

        if not selected:
            raise ValueError(
                f"No Gemini model configured for purpose: "
                f"{purpose}"
            )

        if isinstance(
            selected,
            str
        ):
            return [selected]

        return list(selected)

    # ---------------------------------------------------------
    # TEXT GENERATION
    # ---------------------------------------------------------

    def text(
        self,
        purpose: str,
        prompt: str
    ) -> str:

        if not prompt or not prompt.strip():
            raise ValueError(
                "Gemini prompt is empty."
            )

        models = self._models_for(
            purpose
        )

        last_error = None

        for model in models:

            try:

                result = self.retry(
                    lambda model=model: (
                        self.client.models.generate_content(
                            model=model,
                            contents=prompt
                        )
                    ),
                    "gemini",
                    model=model
                )

                text = getattr(
                    result,
                    "text",
                    None
                )

                if not text:
                    raise RuntimeError(
                        f"Gemini returned an empty response "
                        f"using model {model}."
                    )

                return text.strip()

            except Exception as exc:

                last_error = exc

        if last_error:
            raise last_error

        raise RuntimeError(
            "Gemini generation failed."
        )

    # ---------------------------------------------------------
    # JSON GENERATION
    # ---------------------------------------------------------

    def json(
        self,
        purpose: str,
        prompt: str
    ) -> Dict[str, Any]:

        raw = self.text(
            purpose,
            prompt
        )

        cleaned = self._clean_json(
            raw
        )

        try:

            value = json.loads(
                cleaned
            )

        except json.JSONDecodeError as exc:

            raise ValueError(
                "Gemini returned invalid JSON.\n\n"
                f"JSON error: {exc}\n\n"
                f"Gemini response:\n{raw[:5000]}"
            ) from exc

        if not isinstance(
            value,
            dict
        ):
            raise ValueError(
                "Gemini JSON response must be "
                "a JSON object."
            )

        return value

    # ---------------------------------------------------------
    # JSON CLEANING
    # ---------------------------------------------------------

    @staticmethod
    def _clean_json(
        text: str
    ) -> str:

        text = text.strip()

        # Remove Markdown code fences.
        text = re.sub(
            r"^```json\s*",
            "",
            text,
            flags=re.IGNORECASE
        )

        text = re.sub(
            r"^```\s*",
            "",
            text
        )

        text = re.sub(
            r"\s*```$",
            "",
            text
        )

        text = text.strip()

        # If Gemini added explanatory text before/after
        # the JSON, extract the outermost JSON object.
        if not (
            text.startswith("{")
            and text.endswith("}")
        ):

            start = text.find("{")
            end = text.rfind("}")

            if start >= 0 and end > start:

                text = text[
                    start:end + 1
                ]

        return text.strip()
