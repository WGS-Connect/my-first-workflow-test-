from __future__ import annotations

import json
import re


from google import genai


class Gemini:

    def __init__(self, key, retry, config, voice):
        self.c = genai.Client(api_key=key)
        self.retry = retry
        self.config = config
        self.voice = voice

    def text(self, purpose, prompt):

        models = self.config["models"].get(
            purpose,
            self.config["models"]["script"]
        )

        last_error = None

        for model in models:

            try:

                return self.retry(
                    lambda: self.c.models.generate_content(
                        model=model,
                        contents=prompt
                    ).text,
                    "gemini",
                    model=model
                )

            except Exception as exc:

                last_error = exc

        raise last_error

    def json(self, purpose, prompt):

        text = self.text(purpose, prompt).strip()

        # Remove markdown JSON fences if Gemini adds them.
        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE
        )

        text = re.sub(
            r"\s*```$",
            "",
            text
        )

        # Try normal JSON first.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Fallback: extract first JSON object.
        match = re.search(
            r"\{.*\}",
            text,
            flags=re.DOTALL
        )

        if not match:
            raise ValueError(
                "Gemini did not return valid JSON."
            )

        try:
            return json.loads(match.group(0))

        except json.JSONDecodeError as exc:

            raise ValueError(
                f"Gemini returned invalid JSON: {exc}"
            ) from exc
