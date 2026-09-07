"""Traducción de segmentos.

ClaudeTranslator usa la API de Anthropic con salida estructurada (JSON schema) para
traducir lotes de segmentos, preservando terminología técnica en el idioma original.
MockTranslator permite correr el pipeline sin API key (tests / pruebas de layout).
"""

import json
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
- Return exactly one translation per input id. Never add commentary.
"""


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


class ClaudeTranslator:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        source_lang: str = "English",
        target_lang: str = "Spanish",
        glossary: list[str] | None = None,
        max_batch_chars: int = 6000,
        verbose: bool = True,
        progress_cb: Callable[[int, int], None] | None = None,
    ):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model
        self.max_batch_chars = max_batch_chars
        self.verbose = verbose
        self.progress_cb = progress_cb
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
            self._warn(
                f"Lote de {len(batch)} segmentos rechazado por el clasificador de "
                "seguridad; se conserva el texto original."
            )
            for seg in batch:
                seg.translation = seg.text
            return

        text = next(b.text for b in response.content if b.type == "text")
        by_id = {t["id"]: t["text"] for t in json.loads(text)["translations"]}
        for seg in batch:
            if seg.id in by_id and by_id[seg.id].strip():
                seg.translation = by_id[seg.id]
            else:
                self._warn(f"Segmento {seg.id} sin traducción; se conserva el original.")
                seg.translation = seg.text

    def _warn(self, msg: str) -> None:
        if self.verbose:
            print(f"  aviso: {msg}", file=sys.stderr)
