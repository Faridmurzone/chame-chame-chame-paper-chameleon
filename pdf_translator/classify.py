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
        if not seg.translate:  # pre-marcado en extracción (p. ej. formula-display)
            continue
        reason = _skip_reason(seg, min_chars)
        seg.translate = reason is None
        seg.skip_reason = reason
    resolve_overlaps(segments)
    return segments


def resolve_overlaps(segments: list[Segment], min_overlap: float = 3.0) -> None:
    """Preserva segmentos traducibles cuyo bbox se solapa con otro segmento.

    Los párrafos atravesados por matemática display alta (fracciones, sumatorias)
    quedan fragmentados en bloques con bboxes solapados: re-insertar sus traducciones
    pisaría la fórmula o a los otros fragmentos. Ante un solapamiento real se
    preservan los originales — mejor un párrafo sin traducir que uno corrupto.
    """
    by_page: dict[int, list[Segment]] = {}
    for seg in segments:
        by_page.setdefault(seg.page, []).append(seg)

    for segs in by_page.values():
        for _ in range(10):  # hasta punto fijo (preservar puede crear nuevos conflictos)
            changed = False
            preserved = [s for s in segs if not s.translate]
            translated = [s for s in segs if s.translate]
            for s in translated:
                if any(_overlaps(s.bbox, p.bbox, min_overlap) for p in preserved):
                    s.translate = False
                    s.skip_reason = "solapa-preservado"
                    changed = True
            translated = [s for s in segs if s.translate]
            for i, s in enumerate(translated):
                for t in translated[i + 1:]:
                    if _overlaps(s.bbox, t.bbox, min_overlap):
                        s.translate = t.translate = False
                        s.skip_reason = t.skip_reason = "solapa-fragmento"
                        changed = True
            if not changed:
                break


def _overlaps(a: tuple, b: tuple, min_overlap: float) -> bool:
    """Solapamiento real entre rects (ignora el roce de líneas adyacentes)."""
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    return ix > min_overlap and iy > min_overlap


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
