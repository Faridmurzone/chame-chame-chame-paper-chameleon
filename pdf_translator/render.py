"""Re-inserción de las traducciones en el PDF preservando imágenes, gráficos y fórmulas.

Estrategia: por página, se redacta (borra) solo el texto de los bloques traducidos y se
inserta la traducción en el mismo bounding box con insert_htmlbox, que achica la fuente
si el texto traducido ocupa más que el original.
"""

import html
import sys
from collections import defaultdict

import fitz

from .segments import Segment


def render_translations(doc: fitz.Document, segments: list[Segment], verbose: bool = True) -> None:
    by_page: dict[int, list[Segment]] = defaultdict(list)
    for seg in segments:
        if seg.translate and seg.translation:
            by_page[seg.page].append(seg)

    for pno, segs in sorted(by_page.items()):
        page = doc[pno]
        for seg in segs:
            page.add_redact_annot(fitz.Rect(seg.bbox))
        # Borrar solo texto: imágenes y gráficos vectoriales quedan intactos
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        for seg in segs:
            _insert_segment(page, seg, verbose)


def _insert_segment(page: fitz.Page, seg: Segment, verbose: bool) -> None:
    rect = fitz.Rect(seg.bbox)
    body = html.escape(seg.translation or "")
    if seg.bold:
        body = f"<b>{body}</b>"
    if seg.italic:
        body = f"<i>{body}</i>"
    css = (
        "* {{margin:0; padding:0; font-family: sans-serif; "
        "font-size:{size}px; color:{color}; line-height:1.12;}}"
    ).format(size=seg.font_size, color=seg.color_hex)

    # scale_low=0.5: permite achicar hasta 50% para que el texto entre en el bbox
    spare, scale = page.insert_htmlbox(rect, body, css=css, scale_low=0.5)
    if spare < 0:
        # Ni con 50% entró: reintentar sin límite inferior de escala
        page.insert_htmlbox(rect, body, css=css, scale_low=0)
        if verbose:
            print(
                f"  aviso: segmento {seg.id} (pág. {seg.page + 1}) requirió una "
                "reducción de fuente mayor al 50%.",
                file=sys.stderr,
            )
