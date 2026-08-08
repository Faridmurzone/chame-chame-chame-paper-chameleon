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
        links_before = page.get_links()
        to_redact = [s for s in segs if not s.ocr]
        if to_redact:
            for seg in to_redact:
                page.add_redact_annot(fitz.Rect(seg.bbox))
            # Borrar solo texto: imágenes y gráficos vectoriales quedan intactos
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
        for seg in segs:
            if seg.ocr:
                # Página escaneada: el texto es parte de la imagen — se cubre
                page.draw_rect(fitz.Rect(seg.bbox), color=None, fill=(1, 1, 1))
            _insert_segment(page, seg, verbose)
        _restore_links(page, links_before)


def _link_key(link: dict) -> tuple:
    r = link.get("from", fitz.Rect())
    return (
        link.get("kind"),
        round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1),
        link.get("uri"), link.get("page"), str(link.get("to")),
    )


def _restore_links(page: fitz.Page, links_before: list[dict]) -> None:
    """Re-inserta los hipervínculos que la redacción eliminó."""
    surviving = {_link_key(l) for l in page.get_links()}
    for link in links_before:
        if _link_key(link) not in surviving:
            try:
                page.insert_link(link)
            except Exception:
                pass  # un link irrecuperable no debe abortar el documento


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
