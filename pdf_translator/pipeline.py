"""Orquestación del pipeline: extraer → clasificar → traducir → renderizar."""

import sys
from collections.abc import Callable

import fitz

from .classify import classify_segments
from .extract import extract_segments
from .render import render_translations
from .translate import Translator

# progress(stage, current, total); stage: extract|classify|translate|render|done
ProgressFn = Callable[[str, int, int], None]


def translate_pdf(
    input_path: str,
    output_path: str,
    translator: Translator,
    pages: list[int] | None = None,
    verbose: bool = True,
    progress: ProgressFn | None = None,
) -> dict:
    def emit(stage: str, current: int = 0, total: int = 0) -> None:
        if progress:
            progress(stage, current, total)

    doc = fitz.open(input_path)
    try:
        emit("extract")
        segments = extract_segments(doc, pages=pages)
        emit("classify")
        classify_segments(segments)
        to_translate = [s for s in segments if s.translate]
        skipped = len(segments) - len(to_translate)
        if verbose:
            print(
                f"{len(segments)} bloques de texto: {len(to_translate)} a traducir, "
                f"{skipped} preservados (fórmulas, código, números, URLs).",
                file=sys.stderr,
            )
        if progress and hasattr(translator, "progress_cb"):
            translator.progress_cb = lambda done, total: progress("translate", done, total)
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
        emit("render")
        render_translations(doc, segments, verbose=verbose)
        doc.save(output_path, garbage=3, deflate=True)
    finally:
        doc.close()

    emit("done", len(to_translate), len(segments))
    return {
        "segments": len(segments),
        "translated": len(to_translate),
        "skipped": skipped,
    }
