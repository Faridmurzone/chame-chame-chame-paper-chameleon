"""Clasificación de segmentos: qué se traduce y qué se deja intacto (fórmulas, código, etc.)."""

import re

from .segments import Segment

URL_RE = re.compile(r"^(https?://|www\.|doi:|10\.\d{4,}/)\S+$", re.IGNORECASE)
# Solo números, puntuación y espacios: numeración de páginas, ejes, referencias tipo [12]
NUMERIC_RE = re.compile(r"^[\d\s.,;:%()\[\]\-–—+*/=<>±×·]*$")

MATH_RATIO_THRESHOLD = 0.30
MONO_RATIO_THRESHOLD = 0.60
SYMBOL_DENSITY_THRESHOLD = 0.45


def classify_segments(segments: list[Segment], min_chars: int = 2) -> list[Segment]:
    """Marca cada segmento con translate=True/False y el motivo del salto."""
    for seg in segments:
        reason = _skip_reason(seg, min_chars)
        seg.translate = reason is None
        seg.skip_reason = reason
    return segments


def _skip_reason(seg: Segment, min_chars: int) -> str | None:
    text = seg.text.strip()
    alpha = sum(c.isalpha() for c in text)
    if alpha < min_chars:
        return "sin-texto"
    if NUMERIC_RE.match(text):
        return "numerico"
    if URL_RE.match(text):
        return "url"
    if seg.math_ratio >= MATH_RATIO_THRESHOLD:
        return "formula-fuente"
    if seg.mono_ratio >= MONO_RATIO_THRESHOLD:
        return "codigo"
    if _symbol_density(text) >= SYMBOL_DENSITY_THRESHOLD:
        return "formula-simbolos"
    return None


def _symbol_density(text: str) -> float:
    """Fracción de caracteres que no son prosa (letras latinas, dígitos, puntuación).

    Las letras griegas y los símbolos matemáticos cuentan como 'símbolo': un bloque
    dominado por ellos es casi seguro una fórmula.
    """
    stripped = [c for c in text if not c.isspace()]
    if not stripped:
        return 1.0
    prose = sum(_is_prose_char(c) for c in stripped)
    return 1.0 - prose / len(stripped)


def _is_prose_char(c: str) -> bool:
    if c.isdigit() or c in ".,;:'\"!?()-–—%":
        return True
    # Solo alfabeto latino (incluye acentos): griego y símbolos son señal de fórmula
    o = ord(c)
    return c.isalpha() and (o < 0x0370 or 0x1E00 <= o <= 0x1EFF)
