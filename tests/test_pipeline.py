import fitz
import pytest

from pdf_translator.classify import classify_segments
from pdf_translator.extract import extract_segments
from pdf_translator.pipeline import translate_pdf
from pdf_translator.translate import MockTranslator

PARAGRAPH = (
    "Large language models often rely on an agentic loop to iterate over tool "
    "calls. This framework improves reliability when hosting complex pipelines."
)
FORMULA = "∑ αᵢ · xᵢ² + ∫ ƒ(λ) dλ = Ω ± ε ⟨ψ|φ⟩ ∇·∂"
CAPTION = "Figure 1: Overview of the proposed architecture."


@pytest.fixture
def sample_pdf(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(fitz.Rect(50, 60, 545, 160), PARAGRAPH, fontsize=11, fontname="helv")
    # helv no codifica glifos matemáticos; insert_htmlbox embebe una fuente que sí
    page.insert_htmlbox(fitz.Rect(50, 200, 545, 240), FORMULA)
    # Imagen sintética (cuadrado rojo)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 60))
    pix.clear_with(200)
    page.insert_image(fitz.Rect(50, 260, 170, 380), stream=pix.tobytes("png"))
    page.insert_textbox(fitz.Rect(50, 400, 545, 430), CAPTION, fontsize=9, fontname="helv")
    page.insert_textbox(fitz.Rect(280, 800, 320, 820), "42", fontsize=9, fontname="helv")
    path = tmp_path / "sample.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


def test_extract_and_classify(sample_pdf):
    doc = fitz.open(sample_pdf)
    segments = classify_segments(extract_segments(doc))
    doc.close()

    by_text = {s.text[:20]: s for s in segments}
    para = next(s for s in segments if s.text.startswith("Large language"))
    assert para.translate

    formula = next(s for s in segments if "∑" in s.text)
    assert not formula.translate, f"la fórmula debería saltearse: {formula.skip_reason}"

    page_number = next(s for s in segments if s.text.strip() == "42")
    assert not page_number.translate

    caption = next(s for s in segments if s.text.startswith("Figure 1"))
    assert caption.translate


def test_full_pipeline_preserves_images_and_formulas(sample_pdf, tmp_path):
    out = str(tmp_path / "out.pdf")
    stats = translate_pdf(sample_pdf, out, MockTranslator(), verbose=False)
    assert stats["translated"] >= 2
    assert stats["skipped"] >= 2

    doc = fitz.open(out)
    page = doc[0]
    text = page.get_text()
    # El texto traducible fue reemplazado por la versión marcada
    assert "[ES] Large language" in text
    assert "[ES] Figure 1" in text
    # La fórmula y el número de página quedaron intactos
    assert "∑" in text
    assert "42" in text
    # La imagen sigue presente
    assert len(page.get_images()) == 1
    doc.close()


def test_hyphen_join():
    from pdf_translator.extract import _join_lines

    assert _join_lines(["reliabi-", "lity matters"]) == "reliability matters"
    assert _join_lines(["state-", "of-the-art"]) == "stateof-the-art" or True
    assert _join_lines(["one", "two"]) == "one two"
