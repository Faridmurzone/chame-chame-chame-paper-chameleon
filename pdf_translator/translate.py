"""Traducción de segmentos multi-proveedor.

Todos los proveedores comparten la misma lógica: lotes de segmentos, prompt de
sistema con terminología técnica, salida JSON estructurada y placeholders ⟦n⟧.
Cada subclase solo implementa _call(payload) -> str (texto JSON crudo).

- AnthropicTranslator (anthropic SDK, JSON schema + server-side fallbacks)
- OpenAICompatibleTranslator (openai SDK; sirve también para DeepSeek)
- GoogleTranslator (google-genai SDK)

MockTranslator permite correr el pipeline sin API key (tests / pruebas de layout).
"""

import json
import re
import sys
from collections.abc import Callable
from typing import Protocol

from .segments import Segment

DEFAULT_MODEL = "claude-opus-5"

# Términos que en textos técnicos suelen usarse en inglés y no deben traducirse.
# El usuario puede ampliar la lista con --glossary.
DEFAULT_KEEP_TERMS = [
    "loop", "framework", "hosting", "prompt", "embedding", "token", "pipeline",
    "backend", "frontend", "benchmark", "dataset", "overfitting", "fine-tuning",
    "transformer", "deep learning", "machine learning", "software", "hardware",
    "cloud", "deploy", "debug", "script", "kernel", "buffer", "cache", "batch",
]

SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are a professional translator of academic and technical papers, translating from \
{source} to {target}.

You receive a JSON array of text segments extracted from a PDF. Each segment has an \
"id" and a "text". Translate each segment and return them all.

Rules:
- Preserve the register and precision of academic writing; the result must read \
naturally in {target}.
- Technical terms that practitioners normally use in the original language (e.g. \
"loop", "framework", "hosting", "prompt", "overfitting"{extra_terms}) must be KEPT in \
the original language. If translating such a term genuinely improves readability, \
translate it but keep the original term in parentheses the first time it appears.
- Do NOT translate: proper names, author names, acronyms, citations like [12] or \
(Smith et al., 2020), URLs, identifiers, variable names, inline math and symbols — \
reproduce them verbatim inside the translated sentence.
- Some segments contain placeholders like ⟦0⟧, ⟦1⟧ protecting inline formulas or \
symbols. Keep each placeholder exactly as-is, exactly once, at its natural position \
in the translated sentence. Never translate, drop, merge, or duplicate a placeholder.
- Segment boundaries follow the PDF layout, so a segment may start or end \
mid-sentence. Translate each segment on its own without merging, dropping, or \
reordering segments.
- Return exactly one translation per input id. Never add commentary. \
Respond only with a JSON object like {{"translations": [{{"id": 0, "text": "..."}}]}}.
"""


class RefusalError(Exception):
    """El proveedor rechazó el lote por su clasificador de seguridad."""


class Translator(Protocol):
    def translate(self, segments: list[Segment]) -> None:
        """Completa seg.translation para cada segmento (in-place)."""


class MockTranslator:
    """Marca los textos sin llamar a ninguna API. Útil para tests y probar el layout."""

    def __init__(self, progress_cb: Callable[[int, int], None] | None = None):
        self.progress_cb = progress_cb

    def translate(self, segments: list[Segment]) -> None:
        for i, seg in enumerate(segments, start=1):
            seg.translation = f"[ES] {seg.text}"
            if self.progress_cb:
                self.progress_cb(i, len(segments))


class BaseBatchTranslator:
    """Lógica común: batching, prompt, parseo JSON y reintentos suaves."""

    default_model = ""
    # Nombre del parámetro de límite de tokens que acepta la API del proveedor
    token_param = "max_tokens"

    def __init__(
        self,
        model: str = "",
        source_lang: str = "English",
        target_lang: str = "Spanish",
        glossary: list[str] | None = None,
        max_batch_chars: int = 6000,
        verbose: bool = True,
        progress_cb: Callable[[int, int], None] | None = None,
        api_key: str | None = None,
    ):
        self.model = model or self.default_model
        self.max_batch_chars = max_batch_chars
        self.verbose = verbose
        self.progress_cb = progress_cb
        self.api_key = api_key
        keep_terms = list(dict.fromkeys(DEFAULT_KEEP_TERMS + (glossary or [])))
        extra = ", " + ", ".join(f'"{t}"' for t in keep_terms[4:30]) if len(keep_terms) > 4 else ""
        self.system = SYSTEM_PROMPT.format(
            source=source_lang, target=target_lang, extra_terms=extra
        )

    def translate(self, segments: list[Segment]) -> None:
        total = len(segments)
        done = 0
        for batch in self._batches(segments):
            self._translate_batch(batch)
            done += len(batch)
            if self.progress_cb:
                self.progress_cb(done, total)

    def _batches(self, segments: list[Segment]):
        batch: list[Segment] = []
        chars = 0
        for seg in segments:
            if batch and chars + len(seg.text) > self.max_batch_chars:
                yield batch
                batch, chars = [], 0
            batch.append(seg)
            chars += len(seg.text)
        if batch:
            yield batch

    def _translate_batch(self, batch: list[Segment]) -> None:
        payload = json.dumps(
            [{"id": s.id, "text": s.text} for s in batch], ensure_ascii=False
        )
        try:
            text = self._call(payload)
        except RefusalError:
            self._warn(
                f"Lote de {len(batch)} segmentos rechazado por el clasificador de "
                "seguridad; se conserva el texto original."
            )
            for seg in batch:
                seg.translation = seg.text
            return

        try:
            by_id = {t["id"]: t["text"] for t in json.loads(_extract_json(text))["translations"]}
        except (ValueError, KeyError, TypeError):
            self._warn(
                f"Lote de {len(batch)} segmentos con respuesta no-JSON inválida; "
                "se conserva el texto original."
            )
            for seg in batch:
                seg.translation = seg.text
            return

        for seg in batch:
            if seg.id in by_id and by_id[seg.id].strip():
                seg.translation = by_id[seg.id]
            else:
                self._warn(f"Segmento {seg.id} sin traducción; se conserva el original.")
                seg.translation = seg.text

    def _call(self, payload: str) -> str:
        raise NotImplementedError

    def _warn(self, msg: str) -> None:
        if self.verbose:
            print(f"  aviso: {msg}", file=sys.stderr)


def _extract_json(text: str) -> str:
    """Extrae el objeto JSON de una respuesta que puede venir con fences o texto extra."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


class AnthropicTranslator(BaseBatchTranslator):
    default_model = DEFAULT_MODEL

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import anthropic

        self.client = (
            anthropic.Anthropic(api_key=self.api_key)
            if self.api_key
            else anthropic.Anthropic()
        )

    def _call(self, payload: str) -> str:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": payload}],
        )
        if response.stop_reason == "refusal":
            raise RefusalError
        return next(b.text for b in response.content if b.type == "text")


class OpenAICompatibleTranslator(BaseBatchTranslator):
    """Sirve para OpenAI y para cualquier API compatible (DeepSeek, etc.)."""

    default_model = "gpt-5.1"
    base_url: str | None = None
    token_param = "max_completion_tokens"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from openai import OpenAI

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def _call(self, payload: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system},
                {"role": "user", "content": payload},
            ],
            response_format={"type": "json_object"},
            **{self.token_param: 16000},
        )
        choice = response.choices[0]
        if (choice.finish_reason or "") == "content_filter":
            raise RefusalError
        return choice.message.content or ""


class DeepSeekTranslator(OpenAICompatibleTranslator):
    default_model = "deepseek-chat"
    base_url = "https://api.deepseek.com"
    token_param = "max_tokens"


class GoogleTranslator(BaseBatchTranslator):
    default_model = "gemini-3-pro-preview"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from google import genai

        self.client = (
            genai.Client(api_key=self.api_key) if self.api_key else genai.Client()
        )

    def _call(self, payload: str) -> str:
        from google.genai import types

        response = self.client.models.generate_content(
            model=self.model,
            contents=payload,
            config=types.GenerateContentConfig(
                system_instruction=self.system,
                response_mime_type="application/json",
                temperature=0.0,
                max_output_tokens=16000,
            ),
        )
        text = response.text or ""
        if not text.strip():
            raise RefusalError
        return text


# Compat: el CLI y los tests viejos usan ClaudeTranslator
ClaudeTranslator = AnthropicTranslator

PROVIDER_CLASSES: dict[str, type[BaseBatchTranslator]] = {
    "anthropic": AnthropicTranslator,
    "openai": OpenAICompatibleTranslator,
    "deepseek": DeepSeekTranslator,
    "google": GoogleTranslator,
}

# Variable de entorno donde cada proveedor busca su key por defecto
PROVIDER_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def make_translator(
    provider: str = "anthropic",
    model: str | None = None,
    **kwargs,
) -> BaseBatchTranslator:
    provider = provider if provider in PROVIDER_CLASSES else "anthropic"
    return PROVIDER_CLASSES[provider](model=model or "", **kwargs)
