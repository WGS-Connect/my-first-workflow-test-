from __future__ import annotations

import json
import re
from typing import Any, Dict

from google import genai


class Gemini:
    """
    Gemini client used by the audiobook pipeline.

    Model order:

    1. llm.gemini_primary
    2. llm.gemini_fallbacks

    Example config:

    "llm": {
        "gemini_primary": "gemini-3.5-flash",
        "gemini_fallbacks": [
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite"
        ]
    }

    Retry/backoff is handled by the existing Retry object.
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
        """
        Return Gemini models in primary -> fallback order.

        Preferred configuration:

        "llm": {
            "gemini_primary": "...",
            "gemini_fallbacks": ["...", "..."]
        }

        Backward compatibility is retained for the older
        "models" configuration.
        """

        # -----------------------------------------------------
        # NEW CONFIGURATION
        # -----------------------------------------------------

        llm = self.config.get("llm", {})

        if isinstance(llm, dict):

            primary = llm.get(
                "gemini_primary"
            )

            fallbacks = llm.get(
                "gemini_fallbacks",
                []
            )

            models: list[str] = []

            if primary:
                models.append(
                    str(primary).strip()
                )

            if isinstance(
                fallbacks,
                list
            ):
                for model in fallbacks:

                    model = str(
                        model
                    ).strip()

                    if model and model not in models:
                        models.append(model)

            if models:
                return models

        # -----------------------------------------------------
        # OLD CONFIGURATION
        # -----------------------------------------------------

        models_config = self.config.get(
            "models",
            {}
        )

        if isinstance(
            models_config,
            dict
        ):

            selected = models_config.get(
                purpose
            )

            if not selected:
                selected = models_config.get(
                    "script"
                )

            if selected:

                if isinstance(
                    selected,
                    str
                ):
                    return [
                        selected
                    ]

                if isinstance(
                    selected,
                    list
                ):
                    return [
                        str(model).strip()
                        for model in selected
                        if str(model).strip()
                    ]

        raise ValueError(
            "No Gemini models configured. "
            "Add llm.gemini_primary and "
            "llm.gemini_fallbacks to config.json."
        )

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

                # Continue to the next Gemini model.
                continue

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

        # Remove Markdown JSON code fence.
        text = re.sub(
            r"^```json\s*",
            "",
            text,
            flags=re.IGNORECASE
        )

        # Remove generic Markdown code fence.
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

        # Extract JSON object if Gemini added
        # explanatory text around it.
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
