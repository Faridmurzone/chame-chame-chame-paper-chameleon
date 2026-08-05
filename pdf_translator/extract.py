"""Extracción de bloques de texto con geometría y estilo desde un PDF (PyMuPDF).

Además de extraer, enmascara la matemática inline: los spans con fuentes matemáticas
y los runs de caracteres de bloques Unicode matemáticos/griegos se reemplazan por
placeholders ⟦n⟧ (ver segments.py) para que viajen intactos por la traducción.
"""

from collections import Counter

import fitz

from .segments import MASK_RE, Segment, mask_token

# Fuentes típicamente usadas para matemática en papers (LaTeX y afines)
MATH_FONT_MARKERS = (
    "cmmi", "cmsy", "cmex", "cmbsy", "msam", "msbm", "math", "symbol",
    "esint", "rsfs", "stmary", "wasy", "eufm", "bbold", "dsrom",
)
MONO_FONT_MARKERS = (
    "mono", "courier", "consol", "inconsolata", "cmtt", "lmtt", "typewriter",
    "menlo", "sourcecodepro", "firacode",
)

# Bloques Unicode que en prosa técnica son casi siempre matemática
STRONG_MATH_RANGES = (
    (0x0370, 0x03FF),   # griego
    (0x1D2C, 0x1D7F),   # letras modificadoras sub/superíndice (ᵢ, ᵃ)
    (0x2070, 0x209F),   # super/subíndices numéricos
    (0x2100, 0x214F),   # letterlike (ℝ, ℓ)
    (0x2190, 0x21FF),   # flechas
    (0x2200, 0x22FF),   # operadores matemáticos
    (0x27C0, 0x27EF),   # misc. matemática A
    (0x2980, 0x29FF),   # misc. matemática B
    (0x2A00, 0x2AFF),   # operadores suplementarios
    (0x1D400, 0x1D7FF), # alfanuméricos matemáticos (𝑥, 𝐀)
)


def _font_matches(font: str, markers: tuple[str, ...]) -> bool:
    f = font.lower()
    return any(m in f for m in markers)


def _is_math_span(span: dict) -> bool:
    """Detecta spans de matemática inline dentro de un bloque de prosa."""
    from .classify import _symbol_density

    if _font_matches(span["font"], MATH_FONT_MARKERS):
        return True
    text = span["text"].strip()
    if text and _symbol_density(text) >= 0.5:
        return True
    # Superíndices cortos (exponentes, marcas de nota al pie)
    if span["flags"] & 1 and 0 < len(text) <= 4:
        return True
    return False


def _is_strong_math_char(c: str) -> bool:
    o = ord(c)
    return any(a <= o <= b for a, b in STRONG_MATH_RANGES)


def _span_fragments(span: dict) -> list[tuple[str, bool]]:
    """Parte el texto de un span en fragmentos (texto, es_matemática)."""
    text = span["text"]
    if not text:
        return []
    if _is_math_span(span):
        return [(text, True)]
    frags: list[tuple[str, bool]] = []
    cur, cur_math = "", None
    for c in text:
        m = _is_strong_math_char(c)
        if cur_math is None or m == cur_math:
            cur += c
            cur_math = m
        else:
            frags.append((cur, cur_math))
            cur, cur_math = c, m
    if cur:
        frags.append((cur, cur_math))
    return frags


def _mask_line(spans: list[dict], masks: list[str]) -> str:
    """Construye el texto de una línea reemplazando runs matemáticos por ⟦n⟧.

    Runs matemáticos separados solo por espacios se fusionan en un único placeholder
    (p. ej. '∑ᵢ αᵢ·xᵢ ⩽ Ω' es una sola fórmula).
    """
    parts: list[str] = []
    pending = ""  # run matemático en curso
    ws = ""       # espacios después del run, por si el run continúa

    def flush() -> None:
        nonlocal pending, ws
        if pending:
            parts.append(mask_token(len(masks)))
            masks.append(pending)
            pending = ""
        if ws:
            parts.append(ws)
            ws = ""

    for span in spans:
        for frag, is_math in _span_fragments(span):
            if is_math:
                if pending and ws:
                    pending += ws
                    ws = ""
                pending += frag
            elif not frag.strip():
                if pending:
                    ws += frag
                else:
                    parts.append(frag)
            else:
                flush()
                parts.append(frag)
    flush()
    return "".join(parts).strip()


class _LineInfo:
    """Métricas y texto enmascarado de una línea, para agrupar en segmentos."""

    def __init__(self, line: dict):
        self.bbox = fitz.Rect(line["bbox"])
        self.masks: list[str] = []
        self.text = _mask_line(line["spans"], self.masks)
        self.sizes: Counter = Counter()
        self.colors: Counter = Counter()
        self.fonts: Counter = Counter()
        self.math_chars = 0
        self.mono_chars = 0
        self.bold_chars = 0
        self.italic_chars = 0
        self.total_chars = 0
        for span in line["spans"]:
            text = span["text"]
            if not text:
                continue
            n = len(text)
            self.total_chars += n
            self.sizes[round(span["size"], 1)] += n
            self.colors[span["color"]] += n
            self.fonts[span["font"]] += n
            if _font_matches(span["font"], MATH_FONT_MARKERS):
                self.math_chars += n
            if _font_matches(span["font"], MONO_FONT_MARKERS):
                self.mono_chars += n
            # flags de PyMuPDF: bit 4 = bold, bit 1 = italic
            if span["flags"] & 16:
                self.bold_chars += n
            if span["flags"] & 2:
                self.italic_chars += n

    @property
    def is_display_math(self) -> bool:
        """Línea de matemática display (o mayormente enmascarada): se preserva in-place."""
        if self.total_chars == 0:
            return False
        if self.math_chars / self.total_chars >= 0.5:
            return True
        alpha = sum(c.isalpha() for c in MASK_RE.sub("", self.text))
        return bool(self.masks) and alpha < 3


def extract_segments(doc: fitz.Document, pages: list[int] | None = None) -> list[Segment]:
    """Devuelve un Segment por run de líneas de prosa.

    Dentro de un bloque, las líneas de matemática display no generan segmento: quedan
    intactas en el PDF, y los runs de prosa que las rodean se traducen por separado,
    cada uno con su propio bounding box.
    """
    segments: list[Segment] = []
    seg_id = 0
    page_numbers = pages if pages is not None else range(doc.page_count)

    for pno in page_numbers:
        page = doc[pno]
        data = page.get_text("dict")
        for block in data["blocks"]:
            if block.get("type") != 0:  # 0 = texto; 1 = imagen
                continue
            run: list[_LineInfo] = []
            for line in block["lines"]:
                info = _LineInfo(line)
                if not info.text:
                    continue
                if info.is_display_math:
                    seg_id = _flush_run(run, segments, seg_id, pno)
                    run = []
                    # La línea display genera un segmento preservado: no se traduce,
                    # pero su rect participa en la resolución de solapamientos.
                    segments.append(
                        Segment(
                            id=seg_id,
                            page=pno,
                            bbox=tuple(info.bbox),
                            text=info.text,
                            masks=info.masks,
                            translate=False,
                            skip_reason="formula-display",
                        )
                    )
                    seg_id += 1
                else:
                    run.append(info)
            seg_id = _flush_run(run, segments, seg_id, pno)

    return segments


def _flush_run(run: list["_LineInfo"], segments: list[Segment], seg_id: int, pno: int) -> int:
    """Convierte un run de líneas de prosa en un Segment (si no está vacío)."""
    if not run:
        return seg_id

    masks: list[str] = []
    lines_text: list[str] = []
    for info in run:
        offset = len(masks)
        # Renumerar los placeholders de la línea al espacio del segmento
        text = MASK_RE.sub(lambda m: mask_token(int(m.group(1)) + offset), info.text)
        masks.extend(info.masks)
        lines_text.append(text)

    text = _join_lines(lines_text)
    total = sum(i.total_chars for i in run)
    if not text or total == 0:
        return seg_id

    bbox = run[0].bbox
    for info in run[1:]:
        bbox |= info.bbox
    sizes = sum((i.sizes for i in run), Counter())
    colors = sum((i.colors for i in run), Counter())
    fonts = sum((i.fonts for i in run), Counter())

    segments.append(
        Segment(
            id=seg_id,
            page=pno,
            bbox=tuple(bbox),
            text=text,
            font_size=sizes.most_common(1)[0][0],
            color=colors.most_common(1)[0][0],
            bold=sum(i.bold_chars for i in run) / total > 0.5,
            italic=sum(i.italic_chars for i in run) / total > 0.5,
            fonts=[f for f, _ in fonts.most_common()],
            math_ratio=sum(i.math_chars for i in run) / total,
            mono_ratio=sum(i.mono_chars for i in run) / total,
            masks=masks,
        )
    )
    return seg_id + 1


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
