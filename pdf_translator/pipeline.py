"""Orquestación del pipeline: extraer → clasificar → traducir → renderizar."""

import sys

import fitz

from .classify import classify_segments
from .extract import extract_segments
from .render import render_translations
from .translate import Translator


def translate_pdf(
    input_path: str,
    output_path: str,
    translator: Translator,
    pages: list[int] | None = None,
    ocr_language: str = "eng",
    verbose: bool = True,
    on_progress=None,
) -> dict:
    """on_progress: callback opcional (etapa, hechos, total) para UIs."""

    def report(stage: str, done: int = 0, total: int = 0) -> None:
        if on_progress:
            on_progress(stage, done, total)

    doc = fitz.open(input_path)
    try:
        report("extrayendo")
        segments = extract_segments(doc, pages=pages, ocr_language=ocr_language)
        classify_segments(segments)
        to_translate = [s for s in segments if s.translate]
        skipped = len(segments) - len(to_translate)
        if verbose:
            print(
                f"{len(segments)} bloques de texto: {len(to_translate)} a traducir, "
                f"{skipped} preservados (fórmulas, código, números, URLs).",
                file=sys.stderr,
            )
        report("traduciendo", 0, len(to_translate))
        if on_progress and hasattr(translator, "on_progress"):
            translator.on_progress = lambda d, t: report("traduciendo", d, t)
        _translate_deduped(translator, to_translate)
        report("renderizando")
        for seg in to_translate:
            if seg.translation is None:
                continue
            seg.translation, missing = seg.unmask(seg.translation)
            if missing and verbose:
                print(
                    f"  aviso: segmento {seg.id} — la traducción omitió "
                    f"{len(missing)} fórmula(s); se anexaron al final del bloque.",
                    file=sys.stderr,
                )
        render_translations(doc, segments, verbose=verbose)
        doc.save(output_path, garbage=3, deflate=True)
    finally:
        doc.close()

    return {
        "segments": len(segments),
        "translated": len(to_translate),
        "skipped": skipped,
    }


def _translate_deduped(translator: Translator, segments: list) -> None:
    """Traduce una sola vez los textos repetidos (encabezados por página, etc.)."""
    representatives: dict[str, object] = {}
    unique = []
    for seg in segments:
        if seg.text not in representatives:
            representatives[seg.text] = seg
            unique.append(seg)
    translator.translate(unique)
    for seg in segments:
        if seg.translation is None:
            seg.translation = representatives[seg.text].translation
