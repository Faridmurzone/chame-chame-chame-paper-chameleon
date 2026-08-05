"""Modelo de datos: un segmento es un bloque de texto del PDF con su geometría y estilo."""

from dataclasses import dataclass, field


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

    @property
    def color_hex(self) -> str:
        return f"#{self.color:06x}"
