"""Detección del idioma del documento (para --source auto / origen "Auto" en la UI).

Muestrea los primeros bloques de texto del PDF y detecta el idioma dominante con
langdetect (determinista, seed fija). Si no puede detectar, cae a English.
"""

CODE_TO_NAME = {
    "en": "English",
    "es": "Spanish",
    "pt": "Portuguese",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "ca": "Catalan",
    "gl": "Galician",
    "eu": "Basque",
    "nl": "Dutch",
    "sv": "Swedish",
    "da": "Danish",
    "no": "Norwegian",
    "fi": "Finnish",
    "is": "Icelandic",
    "pl": "Polish",
    "cs": "Czech",
    "sk": "Slovak",
    "sl": "Slovenian",
    "hr": "Croatian",
    "sr": "Serbian",
    "bg": "Bulgarian",
    "ro": "Romanian",
    "hu": "Hungarian",
    "el": "Greek",
    "ru": "Russian",
    "uk": "Ukrainian",
    "tr": "Turkish",
    "he": "Hebrew",
    "ar": "Arabic",
    "fa": "Persian",
    "hi": "Hindi",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "ms": "Malay",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
}

DEFAULT = "English"


def detect_language(texts: list[str], default: str = DEFAULT) -> str:
    """Detecta el idioma dominante de una lista de textos → nombre en inglés."""
    # Solo señales largas: frases cortas ("Fig. 1", números) meten ruido
    sample = " ".join(t.strip() for t in texts if len(t.strip()) >= 40)[:6000]
    if len(sample) < 40:
        return default
    from langdetect import DetectorFactory, detect

    DetectorFactory.seed = 0  # determinismo
    try:
        code = detect(sample)
    except Exception:
        return default
    return CODE_TO_NAME.get(code.split("-")[0], default)


def detect_document_language(path: str, max_pages: int = 3) -> str:
    """Detecta el idioma de un PDF muestreando las primeras páginas."""
    import fitz

    from .extract import extract_segments

    try:
        doc = fitz.open(path)
        pages = list(range(min(max_pages, doc.page_count)))
        segments = extract_segments(doc, pages=pages or None)
        doc.close()
    except Exception:
        return DEFAULT
    return detect_language([s.text for s in segments])
