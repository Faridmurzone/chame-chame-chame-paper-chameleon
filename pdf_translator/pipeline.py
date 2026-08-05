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
    verbose: bool = True,
) -> dict:
    doc = fitz.open(input_path)
    try:
        segments = extract_segments(doc, pages=pages)
        classify_segments(segments)
        to_translate = [s for s in segments if s.translate]
        skipped = len(segments) - len(to_translate)
        if verbose:
            print(
                f"{len(segments)} bloques de texto: {len(to_translate)} a traducir, "
                f"{skipped} preservados (fórmulas, código, números, URLs).",
                file=sys.stderr,
            )
        translator.translate(to_translate)
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
