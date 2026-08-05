"""Modelo de datos: un segmento es un bloque de texto del PDF con su geometría y estilo."""

import re
from dataclasses import dataclass, field

# Placeholders ⟦n⟧ que protegen fórmulas inline durante la traducción
MASK_RE = re.compile(r"⟦(\d+)⟧")


def mask_token(index: int) -> str:
    return f"⟦{index}⟧"


@dataclass
class Segment:
    id: int
    page: int
    bbox: tuple[float, float, float, float]
    text: str
    font_size: float = 10.0
    color: int = 0  # sRGB entero, como lo devuelve PyMuPDF
    bold: bool = False
    italic: bool = False
    fonts: list[str] = field(default_factory=list)
    # Fracción de caracteres renderizados con fuentes matemáticas/simbólicas
    math_ratio: float = 0.0
    # Fracción de caracteres renderizados con fuentes monoespaciadas (código)
    mono_ratio: float = 0.0
    translate: bool = True
    skip_reason: str | None = None
    translation: str | None = None
    # Contenido original de cada placeholder ⟦i⟧ presente en `text`
    masks: list[str] = field(default_factory=list)

    @property
    def color_hex(self) -> str:
        return f"#{self.color:06x}"

    def unmask(self, text: str) -> tuple[str, list[int]]:
        """Restaura las fórmulas enmascaradas en `text`.

        Devuelve (texto_restaurado, indices_perdidos). Si la traducción omitió algún
        placeholder, su contenido se anexa al final para no perder la fórmula.
        """
        if not self.masks:
            return text, []
        seen: set[int] = set()

        def _replace(m: re.Match) -> str:
            i = int(m.group(1))
            if i >= len(self.masks) or i in seen:
                return m.group(0)
            seen.add(i)
            return self.masks[i]

        restored = MASK_RE.sub(_replace, text)
        missing = [i for i in range(len(self.masks)) if i not in seen]
        if missing:
            restored += " " + " ".join(self.masks[i] for i in missing)
        return restored, missing
