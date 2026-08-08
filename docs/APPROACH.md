# Análisis de enfoques para traducir PDFs preservando el layout

Objetivo: traducir el **texto de redacción** de papers (en general EN → ES),
manteniendo imágenes, fórmulas, tablas y diagramas en su lugar. El texto dentro de
imágenes queda fuera de alcance.

## Alternativas evaluadas

### 1. Convertir a otro formato y volver (PDF → DOCX/HTML → traducir → PDF)

Herramientas tipo `pdf2docx` o `pdftohtml` convierten el PDF a un formato editable,
se traduce el texto y se re-exporta.

- ✅ Fácil de traducir una vez convertido.
- ❌ La conversión pierde fidelidad justo en lo que queremos preservar: fórmulas,
  layouts a dos columnas, posicionamiento de figuras. Los papers (casi siempre
  generados con LaTeX) son el peor caso para estos conversores.
- ❌ El PDF re-exportado nunca se ve como el original.

**Descartado**: el requisito principal es justamente la fidelidad visual.

### 2. Reconstrucción vía OCR estructural (Nougat / Marker → Markdown/LaTeX → recompilar)

Modelos como Nougat convierten el PDF a Markdown+LaTeX; se traduce y se recompila.

- ✅ Recupera las fórmulas como LaTeX (editable).
- ❌ Pesado (GPU), con errores de reconocimiento, y la recompilación produce un
  documento *distinto*, no el original traducido. Las figuras hay que re-insertarlas
  a mano.

**Descartado** para v1; puede ser útil a futuro para PDFs escaneados.

### 3. Edición in-place con redacción selectiva (elegido)

Extraer los bloques de texto con coordenadas y estilo (PyMuPDF), borrar **solo el
texto** de los bloques a traducir mediante *redaction annotations* configuradas para
no tocar imágenes ni gráficos vectoriales, e insertar la traducción en el mismo
bounding box con auto-reducción de fuente.

- ✅ Todo lo que no es texto queda intacto por construcción: imágenes, vectores,
  fórmulas y bloques no traducidos ni se tocan.
- ✅ El layout (columnas, posiciones, saltos) se preserva porque cada bloque vuelve a
  su propio rectángulo.
- ✅ Liviano: sin GPU, sin modelos de layout; PyMuPDF hace todo el trabajo de PDF.
- ⚠️ El texto traducido es más largo (~15–20 % ES vs EN): se resuelve con
  `insert_htmlbox(scale_low=...)`, que reduce la fuente hasta que el texto entra.
- ⚠️ Requiere clasificar qué bloques son fórmulas/código para no traducirlos.

Es el mismo enfoque que usan proyectos maduros como **PDFMathTranslate (pdf2zh)**,
validado en la práctica para papers con matemática.

## Decisiones de diseño

### Unidad de traducción: el bloque

PyMuPDF agrupa el texto en bloques ≈ párrafos. Traducir por bloque (y no por línea o
por página) da a Claude contexto suficiente para una redacción natural, y mantiene una
correspondencia 1:1 con un rectángulo de la página para el render.

### Clasificación de fórmulas y código: heurísticas de fuente + densidad de símbolos

Los papers LaTeX renderizan la matemática con fuentes específicas (`cmmi`, `cmsy`,
`msam`, `Symbol`, …) y el código con monoespaciadas. Eso da una señal casi gratuita y
muy precisa:

- `math_ratio ≥ 0.30` (fracción de caracteres en fuentes matemáticas) → fórmula.
- `mono_ratio ≥ 0.60` → código.
- Densidad de símbolos ≥ 0.45 (contando letras griegas como símbolo) → fórmula,
  para PDFs que no usan fuentes LaTeX.
- Solo números/puntuación o URLs → se preserva.

Alternativa considerada: un modelo de detección de layout (DocLayout-YOLO, como usa
pdf2zh). Más preciso para separar fórmulas *display* embebidas en párrafos, pero
agrega una dependencia pesada; queda como mejora futura si las heurísticas se quedan
cortas.

### Traducción: Claude con salida estructurada y prompt de terminología

- Los bloques viajan en lotes (~6 000 caracteres) como JSON `[{id, text}]`, y la
  respuesta se fuerza con **structured outputs** (JSON schema) → parseo garantizado y
  correspondencia por `id` (nunca por posición).
- El *system prompt* fija las reglas de terminología: términos técnicos de uso
  habitual en inglés no se traducen; si se traducen, el original queda entre
  paréntesis. Glosario ampliable por el usuario.
- El system prompt lleva `cache_control` → los lotes de un mismo documento reutilizan
  el prefijo cacheado.
- `fallbacks="default"`: si el clasificador de seguridad rechaza un lote (papers de
  ciberseguridad/biología pueden disparar falsos positivos), la API re-sirve la
  petición con un modelo alternativo; como último recurso el lote conserva el texto
  original.

### Render: redacción selectiva + `insert_htmlbox`

- `apply_redactions(images=NONE, graphics=NONE, text=REMOVE)` borra el texto sin
  tocar nada más.
- `insert_htmlbox` maneja Unicode completo (acentos del español) con fuentes
  embebidas, respeta negrita/itálica/color/tamaño vía CSS y reduce la escala hasta
  que el texto entra en el rectángulo original.

### Fórmulas inline: placeholders ⟦n⟧

La matemática inline se detecta en dos niveles durante la extracción:

- **Por span**: fuentes matemáticas (cmmi, cmsy, …), densidad de símbolos ≥ 0.5, o
  superíndices cortos (exponentes, notas al pie).
- **Por carácter**: runs de caracteres de bloques Unicode matemáticos (griego,
  operadores, flechas, sub/superíndices, alfanuméricos matemáticos), fusionando runs
  separados solo por espacios.

Cada run se reemplaza por un placeholder `⟦n⟧`; el prompt instruye conservarlos
exactamente una vez en su posición natural, y tras traducir se restaura el contenido
original. Si el modelo omite un placeholder, su contenido se anexa al final del
bloque (no se pierde) y se emite un aviso.

Las líneas de **matemática display** (≥ 50 % de caracteres en fuentes matemáticas, o
casi todo enmascarado) no generan segmento traducible: quedan intactas en el PDF con
su tipografía original. Como los párrafos atravesados por matemática alta (fracciones)
quedan fragmentados en bloques con bboxes solapados, un pase de resolución de
conflictos preserva cualquier segmento traducible que se solape con un segmento
preservado o con otro fragmento — evita corromper la zona a cambio de dejar ese
párrafo en el idioma original.

### Tablas

`page.find_tables()` detecta tablas por líneas vectoriales. Se filtran falsos
positivos (más de 15 columnas suele ser una figura). Los bloques dentro del área de
una tabla se descartan y se reemplazan por un segmento por celda, que se traduce
dentro del rect de la celda (con todo el ancho/alto restante para absorber la
expansión del texto). Las celdas numéricas y matemáticas quedan preservadas por la
clasificación normal.

### Hipervínculos

`apply_redactions` destruye los links que tocan las zonas redactadas (en un paper de
prueba: 88 → 2). Se capturan con `page.get_links()` antes de redactar y se
re-insertan los perdidos después, comparando por rect + destino.

### OCR (PDFs escaneados)

Una página con < 30 caracteres de texto extraíble pero con imágenes se considera
escaneada: se extrae con `get_textpage_ocr` (Tesseract, 300 dpi, idioma según
`--source`) y produce la misma estructura de dict que una página normal, así que el
resto del pipeline no cambia. La diferencia está en el render: no hay texto que
redactar (es parte de la imagen), así que se cubre el bbox con un rect blanco y se
inserta la traducción encima.

### Glosario por documento (dos pasadas)

Antes de traducir, un pase con `claude-haiku-4-5` recibe una muestra del texto
(24 000 caracteres) y devuelve: términos que se mantienen en el idioma original y
términos recurrentes con su traducción fija. Eso se inyecta en el system prompt de
todos los lotes → el mismo término se traduce igual en la página 2 y en la 9. Si el
pase falla, se sigue sin glosario (best-effort).

### Dedupe

Los segmentos con texto idéntico (encabezados que se repiten en cada página) se
traducen una sola vez y la traducción se copia al resto.

## Interfaz web

`pdf_translator/web.py` (FastAPI) + una página estática. Los jobs corren en un
thread con estado en memoria (herramienta local, no servicio multiusuario): subida
multipart → job id → polling de estado (etapa + bloques traducidos/total) →
descarga. La API key sale del env del servidor o del formulario.

## Mejoras futuras (en orden sugerido)

1. **Reordenar y fusionar fragmentos solapados**: los párrafos partidos por
   matemática display hoy se preservan; se podrían fusionar sus fragmentos en un solo
   segmento con reflow para traducirlos también.
2. **Modo bilingüe**: páginas original/traducida intercaladas o lado a lado.
3. **Batch API** de Anthropic para documentos largos (50 % de descuento, sin apuro).
4. **Fuente serif** cuando el original es serif, para mayor fidelidad visual.
5. **Estimación de costo** previa con `count_tokens`.
6. **Modelo de layout** (DocLayout-YOLO) como clasificador opcional de mayor precisión.
