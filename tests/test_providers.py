"""Tests del sistema multi-proveedor de traducción."""

import pytest

from pdf_translator.lang_detect import detect_document_language, detect_language
from pdf_translator.translate import (
    AnthropicTranslator,
    DeepSeekTranslator,
    GoogleTranslator,
    MockTranslator,
    OpenAICompatibleTranslator,
    _extract_json,
    make_translator,
)

ENGLISH = (
    "The attention mechanism allows models to draw global dependencies between "
    "inputs, regardless of their distance in the sequence. This framework became "
    "the standard architecture for machine translation benchmarks."
)
SPANISH = (
    "El mecanismo de atención permite a los modelos capturar dependencias globales "
    "entre las entradas de la secuencia, sin importar la distancia entre ellas. "
    "Esta arquitectura se convirtió en el estándar para los benchmarks de traducción."
)


def test_detect_language_english():
    assert detect_language([ENGLISH]) == "English"


def test_detect_language_spanish():
    assert detect_language([SPANISH]) == "Spanish"


def test_detect_language_short_noise_falls_back():
    # Solo ruido corto (números, captions) → default
    assert detect_language(["42", "Fig. 1", "a", "b c"]) == "English"


def test_detect_document_language(tmp_path):
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(fitz.Rect(50, 60, 545, 300), SPANISH, fontsize=11, fontname="helv")
    path = str(tmp_path / "es.pdf")
    doc.save(path)
    doc.close()
    assert detect_document_language(path) == "Spanish"


def test_make_translator_selects_provider():
    assert isinstance(make_translator("anthropic", api_key="x"), AnthropicTranslator)
    assert isinstance(make_translator("openai", api_key="x"), OpenAICompatibleTranslator)
    assert isinstance(make_translator("deepseek", api_key="x"), DeepSeekTranslator)
    assert isinstance(make_translator("google", api_key="x"), GoogleTranslator)


def test_make_translator_default_models():
    assert make_translator("anthropic", api_key="x").model == "claude-opus-5"
    assert make_translator("openai", api_key="x").model == "gpt-5.1"
    assert make_translator("deepseek", api_key="x").model == "deepseek-chat"
    assert make_translator("google", api_key="x").model == "gemini-3-pro-preview"


def test_make_translator_unknown_provider_falls_back():
    assert isinstance(make_translator("no-existe", api_key="x"), AnthropicTranslator)


def test_deepseek_uses_openai_compatible_base_url():
    t = DeepSeekTranslator(api_key="x")
    assert t.base_url == "https://api.deepseek.com"
    assert t.token_param == "max_tokens"


def test_extract_json_variants():
    assert _extract_json('{"translations": []}') == '{"translations": []}'
    assert _extract_json('```json\n{"translations": []}\n```') == '{"translations": []}'
    assert _extract_json('Salida: {"translations": []} fin') == '{"translations": []}'


def test_batch_keeps_originals_on_invalid_json(monkeypatch):
    t = AnthropicTranslator(api_key="x", verbose=False)
    monkeypatch.setattr(t, "_call", lambda payload: "esto no es json")
    from pdf_translator.segments import Segment

    seg = Segment(id=0, page=0, bbox=(0, 0, 1, 1), text="Hello world.")
    t.translate([seg])
    assert seg.translation == "Hello world."  # se conserva el original


def test_batch_progress_and_mock():
    calls = []
    m = MockTranslator(progress_cb=lambda done, total: calls.append((done, total)))
    from pdf_translator.segments import Segment

    segs = [Segment(id=i, page=0, bbox=(0, 0, 1, 1), text=f"texto {i}") for i in range(3)]
    m.translate(segs)
    assert calls == [(1, 3), (2, 3), (3, 3)]
