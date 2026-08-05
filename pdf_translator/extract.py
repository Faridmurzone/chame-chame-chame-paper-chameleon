"""Extracción de bloques de texto con geometría y estilo desde un PDF (PyMuPDF)."""

from collections import Counter

import fitz

from .segments import Segment

# Fuentes típicamente usadas para matemática en papers (LaTeX y afines)
MATH_FONT_MARKERS = (
    "cmmi", "cmsy", "cmex", "cmbsy", "msam", "msbm", "math", "symbol",
    "esint", "rsfs", "stmary", "wasy", "eufm", "bbold", "dsrom",
)
MONO_FONT_MARKERS = (
    "mono", "courier", "consol", "inconsolata", "cmtt", "lmtt", "typewriter",
    "menlo", "sourcecodepro", "firacode",
)


def _font_matches(font: str, markers: tuple[str, ...]) -> bool:
    f = font.lower()
    return any(m in f for m in markers)


def extract_segments(doc: fitz.Document, pages: list[int] | None = None) -> list[Segment]:
    """Devuelve un Segment por bloque de texto, con estilo dominante y métricas de fuente."""
    segments: list[Segment] = []
    seg_id = 0
    page_numbers = pages if pages is not None else range(doc.page_count)

    for pno in page_numbers:
        page = doc[pno]
        data = page.get_text("dict")
        for block in data["blocks"]:
            if block.get("type") != 0:  # 0 = texto; 1 = imagen
                continue
            lines_text: list[str] = []
            sizes: Counter = Counter()
            colors: Counter = Counter()
            fonts: Counter = Counter()
            math_chars = 0
            mono_chars = 0
            bold_chars = 0
            italic_chars = 0
            total_chars = 0

            for line in block["lines"]:
                spans_text = []
                for span in line["spans"]:
                    text = span["text"]
                    if not text:
                        continue
                    spans_text.append(text)
                    n = len(text)
                    total_chars += n
                    sizes[round(span["size"], 1)] += n
                    colors[span["color"]] += n
                    fonts[span["font"]] += n
                    if _font_matches(span["font"], MATH_FONT_MARKERS):
                        math_chars += n
                    if _font_matches(span["font"], MONO_FONT_MARKERS):
                        mono_chars += n
                    # flags de PyMuPDF: bit 4 = bold, bit 1 = italic
                    if span["flags"] & 16:
                        bold_chars += n
                    if span["flags"] & 2:
                        italic_chars += n
                line_text = "".join(spans_text).strip()
                if line_text:
                    lines_text.append(line_text)

            text = _join_lines(lines_text)
            if not text or total_chars == 0:
                continue

            segments.append(
                Segment(
                    id=seg_id,
                    page=pno,
                    bbox=tuple(block["bbox"]),
                    text=text,
                    font_size=sizes.most_common(1)[0][0],
                    color=colors.most_common(1)[0][0],
                    bold=bold_chars / total_chars > 0.5,
                    italic=italic_chars / total_chars > 0.5,
                    fonts=[f for f, _ in fonts.most_common()],
                    math_ratio=math_chars / total_chars,
                    mono_ratio=mono_chars / total_chars,
                )
            )
            seg_id += 1

    return segments


def _join_lines(lines: list[str]) -> str:
    """Une líneas de un bloque reparando palabras cortadas con guion a fin de línea."""
    out = ""
    for line in lines:
        if not out:
            out = line
        elif out.endswith("-") and out[-2:-1].islower() and line[:1].islower():
            out = out[:-1] + line
        else:
            out += " " + line
    return out.strip()
