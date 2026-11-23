"""Read scanned pages with the local Typhoon OCR model in Ollama."""

from base64 import b64encode
from hashlib import sha256
from http.client import HTTPConnection, HTTPException
from io import BytesIO
import json
import os
import re
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlsplit

from .capabilities import ensure_typhoon_available
from .errors import ConversionError

if TYPE_CHECKING:
    from PIL.Image import Image


MODEL = "scb10x/typhoon-ocr1.5-3b"
PULL_COMMAND = f"ollama pull {MODEL}"
MAX_REPLY_BYTES = 1024 * 1024
# Typhoon OCR v1.5 prompt with figure_language="Thai" (Apache-2.0).
# Source: https://github.com/scb-10x/typhoon-ocr/blob/9727f54627e6bd812df66bc2aea1fe11cd6be3f0/packages/typhoon_ocr/typhoon_ocr/ocr_utils.py#L380
# See LICENSE.typhoon-ocr.
PROMPT = """Extract all text from the image.

Instructions:
- Only return the clean Markdown.
- Do not include any explanation or extra text.
- You must include all information on the page.

Formatting Rules:
- Tables: Render tables using <table>...</table> in clean HTML format.
- Equations: Render equations using LaTeX syntax with inline ($...$) and block ($$...$$).
- Images/Charts/Diagrams: Wrap any clearly defined visual areas (e.g. charts, diagrams, pictures) in:

<figure>
Describe the image's main elements (people, objects, text), note any contextual clues (place, event, culture), mention visible text and its meaning, provide deeper analysis when relevant (especially for financial charts, graphs, or documents), comment on style or architecture if relevant, then give a concise overall summary. Describe in Thai.
</figure>
- Page Numbers: Wrap page numbers in <page_number>...</page_number> (e.g., <page_number>14</page_number>).
- Checkboxes: Use ☐ for unchecked and ☑ for checked boxes.
"""
OPTIONS = {
    "temperature": 0.1,
    "top_p": 0.6,
    "repeat_penalty": 1.1,
    "num_ctx": 8192,
    "num_predict": 5500,
    "seed": 42,
}
PageCache = Callable[[str, Callable[[], str]], str]


def local_endpoint() -> tuple[str, int]:
    endpoint = os.environ.get("OLLAMA_HOST") or "127.0.0.1:11434"
    try:
        url = urlsplit(endpoint if "://" in endpoint else f"http://{endpoint}")
        host = url.hostname
        port = 11434 if url.port is None else url.port
        if (
            url.scheme == "http"
            and host in {"127.0.0.1", "localhost", "::1"}
            and url.username is None
            and url.path in {"", "/"}
            and not url.query
            and not url.fragment
            and port > 0
        ):
            return ("127.0.0.1" if host == "localhost" else host), port
    except ValueError:
        pass
    raise ConversionError(
        "Typhoon requires a loopback Ollama address such as "
        "OLLAMA_HOST=127.0.0.1:11434"
    )


class Typhoon:
    def __init__(self, cache: PageCache | None = None):
        ensure_typhoon_available()
        self.host, self.port = local_endpoint()
        self.cache = cache
        self.digest: str | None = None
        self.page_number = 0

    def _request(self, route: str, body: bytes | None = None) -> dict:
        # HTTPConnection opens a direct socket and does not follow redirects.
        connection = HTTPConnection(self.host, self.port, timeout=300)
        try:
            if body is None:
                connection.request("GET", route)
            else:
                connection.request(
                    "POST", route, body, {"Content-Type": "application/json"}
                )
            with connection.getresponse() as response:
                if response.status == 404:
                    raise ConversionError(f"Typhoon model unavailable. Run: {PULL_COMMAND}")
                if response.status != 200:
                    raise ConversionError(f"Ollama returned HTTP {response.status}")
                data = response.read(MAX_REPLY_BYTES + 1)
        except (OSError, HTTPException) as exc:
            raise ConversionError(
                f"Ollama request failed: {exc}. Check that Ollama is running"
            ) from exc
        finally:
            connection.close()

        if len(data) > MAX_REPLY_BYTES:
            raise ConversionError("Ollama reply exceeded the size limit")
        try:
            result = json.loads(data)
        except (ValueError, RecursionError) as exc:
            raise ConversionError("Ollama returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ConversionError("Ollama returned an invalid reply")
        return result

    def _model_digest(self) -> str:
        models = self._request("/api/tags").get("models")
        if not isinstance(models, list):
            raise ConversionError("Ollama returned an invalid model list")
        for model in models:
            if isinstance(model, dict) and model.get("name") in (
                MODEL, f"{MODEL}:latest"
            ):
                digest = model.get("digest")
                if isinstance(digest, str) and digest:
                    return digest
                raise ConversionError("Ollama did not report a Typhoon model digest")
        raise ConversionError(f"Typhoon model unavailable. Run: {PULL_COMMAND}")

    def _recognize(self, body: bytes) -> str:
        reply = self._request("/api/chat", body)
        if reply.get("done") is not True or reply.get("done_reason") != "stop":
            raise ConversionError("Ollama did not finish transcribing the page")
        message = reply.get("message")
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str):
            raise ConversionError("Ollama did not return page text")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ConversionError("Ollama returned invalid Unicode") from exc
        text = text.strip()
        fence = re.fullmatch(r"```(?:markdown|md)[ \t]*\r?\n(.*?)\r?\n```", text, re.DOTALL)
        return fence[1].strip() if fence else text

    def _stable_model_digest(self) -> str:
        digest = self._model_digest()
        if self.digest is None:
            self.digest = digest
        elif self.digest != digest:
            raise ConversionError("the Ollama model changed during OCR; rerun the conversion")
        return digest

    def __call__(self, image: "Image") -> str:
        self.page_number += 1
        try:
            image.thumbnail((1536, 1536))
            with BytesIO() as buffer:
                image.save(buffer, format="JPEG", quality=95)
                encoded = b64encode(buffer.getvalue()).decode("ascii")
            body = json.dumps({
                "model": MODEL,
                "messages": [{"role": "user", "content": PROMPT, "images": [encoded]}],
                "options": OPTIONS,
                "stream": False,
            }).encode("utf-8")
            if self.cache is None:
                return self._recognize(body)
            digest = self._stable_model_digest()
            key = sha256(digest.encode("utf-8") + b"\0" + body).hexdigest()

            def recognize() -> str:
                text = self._recognize(body)
                self._stable_model_digest()
                return text

            return self.cache(key, recognize)
        except ConversionError as exc:
            raise ConversionError(f"Typhoon page {self.page_number}: {exc}") from exc
