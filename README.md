# pdf-translator

Traduce PDFs — principalmente papers académicos — manteniendo el layout original:
las imágenes, fórmulas, tablas y gráficos quedan exactamente donde estaban, y solo
el texto de redacción se traduce.

```
pdf-translate paper.pdf --to es
# genera paper.es.pdf
```

También tiene **interfaz web**: subís el PDF desde el navegador, seguís el progreso
en vivo y descargás el resultado.

```bash
pip install -e ".[web]"
pdf-translate-web          # → http://127.0.0.1:8000
```

La página (en `pdf_translator/static/index.html`) es self-contained: sin build ni
frameworks, tema oscuro. El backend expone `POST /api/translate`,
`GET /api/jobs/{id}` (progreso por etapa y bloque) y `GET /api/jobs/{id}/download`.

## Cómo funciona

El pipeline trabaja **in-place** sobre el PDF original, en cuatro etapas:

1. **Extracción** (`extract.py`): con PyMuPDF se extrae cada bloque de texto con su
   *bounding box*, tamaño de fuente, color, negrita/itálica y las fuentes usadas.
2. **Clasificación** (`classify.py`): heurísticas deciden qué bloques se traducen y
   cuáles se preservan tal cual:
   - bloques renderizados con fuentes matemáticas (cmmi, cmsy, msam, Symbol, …) → fórmula
   - alta densidad de símbolos/letras griegas → fórmula
   - fuentes monoespaciadas dominantes → código
   - solo números/puntuación (números de página, ejes) o URLs/DOIs → se preservan
   - segmentos cuyo bbox se solapa con una fórmula preservada → se preservan
     (mejor un párrafo sin traducir que uno corrupto)

   Además, la **matemática inline** dentro de un párrafo (variables, símbolos,
   letras griegas, superíndices) se **enmascara con placeholders** `⟦n⟧` antes de
   traducir y se restaura verbatim después, así nunca la toca el traductor. Las
   líneas de matemática *display* directamente no se tocan: quedan en el PDF con su
   tipografía original.
3. **Traducción** (`translate.py`): los bloques se envían en lotes a la API de Claude
   con salida estructurada (JSON), con instrucciones de terminología (ver abajo).
4. **Render** (`render.py`): se *redacta* (borra) únicamente el texto de los bloques
   traducidos — las imágenes y los gráficos vectoriales no se tocan — y se inserta la
   traducción en el mismo bounding box, reduciendo la fuente automáticamente si el
   texto traducido ocupa más que el original (el español suele ser ~15–20 % más largo
   que el inglés).

El análisis de alternativas (OCR + reconstrucción, conversión a HTML/DOCX, modelos de
layout, etc.) y por qué se eligió este enfoque está en
[`docs/APPROACH.md`](docs/APPROACH.md).

## Terminología técnica

Los papers técnicos usan términos que en español se dejan en inglés ("loop",
"framework", "hosting", "prompt", "overfitting", …). El prompt de traducción instruye:

- **mantener en inglés** los términos que la comunidad usa habitualmente sin traducir;
- si se traduce un término para mejorar la legibilidad, **conservar el original entre
  paréntesis** la primera vez que aparece;
- nunca tocar citas (`[12]`, `(Smith et al., 2020)`), siglas, nombres propios, URLs,
  identificadores ni matemática inline.

Se puede ampliar la lista con un glosario propio: `--glossary terminos.txt`
(un término por línea).

## Instalación

```bash
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
```

## Uso

```bash
# Traducción completa a español (default)
pdf-translate paper.pdf

# Opciones
pdf-translate paper.pdf -o salida.pdf --to es --source en
pdf-translate paper.pdf --pages 1-3,7          # solo algunas páginas
pdf-translate paper.pdf --glossary terminos.txt
pdf-translate paper.pdf --model claude-opus-5  # modelo a usar

# Modo mock: no llama a la API, marca los bloques con [ES].
# Útil para verificar qué se traduciría y cómo queda el layout, gratis.
pdf-translate paper.pdf --mock
```

El traductor usa `claude-opus-5` por defecto e incluye *server-side fallbacks*
(`fallbacks="default"`): si el clasificador de seguridad de la API rechaza un lote
(puede pasar con papers de seguridad informática o biología), la petición se re-sirve
automáticamente con un modelo alternativo; si aún así se rechaza, ese lote conserva el
texto original en lugar de romper el documento.

## Estado y limitaciones conocidas

- **Párrafos atravesados por matemática display alta** (fracciones, sumatorias con
  límites): esos párrafos quedan fragmentados en el PDF con cajas solapadas, así que
  se preservan completos en el idioma original en lugar de arriesgar un render
  corrupto. Suelen ser pocos por paper.
- **Matemática inline re-renderizada**: los símbolos restaurados desde placeholders
  se re-insertan con la fuente del bloque (no con la fuente matemática original);
  el contenido es idéntico pero el glifo puede diferir levemente.
- **Tipografía**: el texto traducido se inserta con una fuente sans-serif embebida,
  no con la fuente original del paper (las fuentes de los PDFs suelen ser subconjuntos
  no reutilizables). Se preservan tamaño, color, negrita e itálica.
- **Texto dentro de imágenes**: no se traduce (fuera de alcance por ahora).
- PDFs escaneados (sin capa de texto) requieren OCR previo — no soportado aún.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Los tests generan PDFs sintéticos y corren el pipeline completo con el traductor mock,
verificando que las fórmulas, números e imágenes se preservan.
