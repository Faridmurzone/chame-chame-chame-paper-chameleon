"""Traducción de segmentos.

ClaudeTranslator usa la API de Anthropic con salida estructurada (JSON schema) para
traducir lotes de segmentos, preservando terminología técnica en el idioma original.
MockTranslator permite correr el pipeline sin API key (tests / pruebas de layout).
"""

import json
import sys
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

GLOSSARY_MODEL = "claude-haiku-4-5"

GLOSSARY_SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {"type": "array", "items": {"type": "string"}},
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "translation": {"type": "string"},
                },
                "required": ["term", "translation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["keep", "translations"],
    "additionalProperties": False,
}

GLOSSARY_PROMPT = """\
You are preparing a terminology sheet for translating a technical/academic paper \
from {source} to {target}. From the paper text you receive, produce:

1. "keep": technical terms appearing in the text that practitioners normally leave \
untranslated in {target} (borrowed terms the community uses as-is).
2. "translations": recurring technical terms that SHOULD be translated, each with \
the exact {target} translation to use consistently throughout the document.

Only include terms that actually appear in the text. At most 30 entries in total. \
Do not include proper names, acronyms, or citations.
"""

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

    def translate(self, segments: list[Segment]) -> None:
        for seg in segments:
            seg.translation = f"[ES] {seg.text}"


class ClaudeTranslator:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        source_lang: str = "English",
        target_lang: str = "Spanish",
        glossary: list[str] | None = None,
        document_glossary: bool = True,
        max_batch_chars: int = 6000,
        api_key: str | None = None,
        verbose: bool = True,
    ):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.document_glossary = document_glossary
        self.max_batch_chars = max_batch_chars
        self.verbose = verbose
        self.user_keep_terms = glossary or []
        self.system = self._build_system(self.user_keep_terms, [])

    def _build_system(self, keep_terms: list[str], fixed: list[dict]) -> str:
        keep = list(dict.fromkeys(DEFAULT_KEEP_TERMS + keep_terms))
        extra = ", " + ", ".join(f'"{t}"' for t in keep[4:40]) if len(keep) > 4 else ""
        system = SYSTEM_PROMPT.format(
            source=self.source_lang, target=self.target_lang, extra_terms=extra
        )
        if fixed:
            table = "\n".join(f'- "{e["term"]}" -> "{e["translation"]}"' for e in fixed)
            system += (
                "\n- Translate these recurring terms consistently, always with the "
                f"given translation:\n{table}\n"
            )
        return system

    # Callback opcional (done, total) para reportar avance (lo usa la web UI)
    on_progress = None

    def translate(self, segments: list[Segment]) -> None:
        if self.document_glossary and segments:
            self._apply_document_glossary(segments)
        done = 0
        for batch in self._batches(segments):
            self._translate_batch(batch)
            done += len(batch)
            if self.on_progress:
                self.on_progress(done, len(segments))

    def _apply_document_glossary(self, segments: list[Segment]) -> None:
        """Pase previo barato: extrae la terminología del documento para traducirla
        de forma consistente en todos los lotes."""
        sample = "\n".join(s.text for s in segments)[:24000]
        try:
            response = self.client.messages.create(
                model=GLOSSARY_MODEL,
                max_tokens=2000,
                system=GLOSSARY_PROMPT.format(
                    source=self.source_lang, target=self.target_lang
                ),
                output_config={"format": {"type": "json_schema", "schema": GLOSSARY_SCHEMA}},
                messages=[{"role": "user", "content": sample}],
            )
            if response.stop_reason == "refusal":
                raise RuntimeError("refusal")
            text = next(b.text for b in response.content if b.type == "text")
            data = json.loads(text)
        except Exception as e:
            self._warn(f"no se pudo generar el glosario del documento ({e}); sigo sin él.")
            return
        keep = self.user_keep_terms + data.get("keep", [])
        fixed = data.get("translations", [])
        self.system = self._build_system(keep, fixed)
        if self.verbose:
            print(
                f"  glosario del documento: {len(data.get('keep', []))} términos se "
                f"mantienen, {len(fixed)} con traducción fija.",
                file=sys.stderr,
            )

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
