import io
import os
import tempfile
import time

os.environ.setdefault("PDF_TRANSLATE_DATA_DIR", tempfile.mkdtemp(prefix="pdf-tr-web-tests-"))

import fitz
import pytest
from fastapi.testclient import TestClient

from pdf_translator.web import app


@pytest.fixture
def client():
    return TestClient(app)


def _pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(
        fitz.Rect(50, 60, 545, 160),
        "Large language models rely on an agentic loop.",
        fontsize=11,
        fontname="helv",
    )
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _wait_done(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        data = client.get(f"/api/jobs/{job_id}").json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(0.05)
    raise AssertionError("timeout esperando el job")


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "pdf-translator" in r.text
    assert "api/translate" in r.text


def test_meta(client):
    r = client.get("/api/meta")
    assert r.status_code == 200
    data = r.json()
    assert "es" in data["languages"]
    provider_ids = [p["id"] for p in data["providers"]]
    assert provider_ids == ["anthropic", "openai", "google", "deepseek"]
    # catálogo de modelos con default marcado por proveedor
    for pid in provider_ids:
        models = data["models"][pid]
        assert models and models[0]["default"]
    assert data["models"]["anthropic"][0]["id"] == "claude-opus-5"
    assert data["models"]["deepseek"][0]["id"] == "deepseek-chat"


def test_translate_flow_mock(client):
    r = client.post(
        "/api/translate",
        data={"mock": "true", "to": "es"},
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    data = _wait_done(client, job_id)
    assert data["status"] == "done", data
    assert data["stage"] == "done"
    assert "traducidos" in data["message"]

    dl = client.get(f"/api/jobs/{job_id}/download")
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/pdf"
    doc = fitz.open(stream=dl.content, filetype="pdf")
    assert "[ES]" in doc[0].get_text()
    doc.close()


def test_job_status_hides_internal_fields(client):
    r = client.post(
        "/api/translate",
        data={"mock": "true"},
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    job_id = r.json()["job_id"]
    data = _wait_done(client, job_id)
    assert "output" not in data and "created" not in data
    # pages llega para el pager del visor
    assert data["pages"] == 1


def test_original_download(client):
    r = client.post(
        "/api/translate",
        data={"mock": "true"},
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    job_id = r.json()["job_id"]
    _wait_done(client, job_id)
    orig = client.get(f"/api/jobs/{job_id}/original")
    assert orig.status_code == 200
    assert orig.headers["content-type"] == "application/pdf"
    assert "inline" in orig.headers["content-disposition"]
    doc = fitz.open(stream=orig.content, filetype="pdf")
    assert "agentic loop" in doc[0].get_text()
    doc.close()


def test_rejects_non_pdf(client):
    r = client.post(
        "/api/translate",
        data={"mock": "true"},
        files={"file": ("x.txt", b"nope", "text/plain")},
    )
    assert r.status_code == 400


def test_rejects_corrupt_pdf(client):
    r = client.post(
        "/api/translate",
        data={"mock": "true"},
        files={"file": ("x.pdf", b"esto no es un pdf", "application/pdf")},
    )
    assert r.status_code == 400


def test_rejects_bad_pages_spec(client):
    r = client.post(
        "/api/translate",
        data={"mock": "true", "pages": "abc"},
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 400


def test_unknown_job_404(client):
    assert client.get("/api/jobs/ffffff").status_code == 404
    assert client.get("/api/jobs/ffffff/download").status_code == 404


def _unique_pdf_bytes(tag: str) -> bytes:
    """PDF con contenido único para no colisionar con el dedup entre corridas."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(
        fitz.Rect(50, 60, 545, 200),
        f"Unique test document {tag} about large language models and agents.",
        fontsize=11,
        fontname="helv",
    )
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def test_history_dedup_and_delete(client):
    data = _unique_pdf_bytes(f"{time.time()}")
    r1 = client.post(
        "/api/translate",
        data={"mock": "true", "to": "es"},
        files={"file": ("hist.pdf", data, "application/pdf")},
    )
    job_id = r1.json()["job_id"]
    _wait_done(client, job_id)

    # El historial lo lista
    hist = client.get("/api/history").json()
    entry = next((h for h in hist if h["id"] == job_id), None)
    assert entry is not None
    assert entry["filename"] == "hist.pdf"
    assert entry["to"] == "es" and entry["pages"] == 1

    # Re-subir el mismo archivo → dedup: mismo job, sin re-traducir
    r2 = client.post(
        "/api/translate",
        data={"mock": "true", "to": "es"},
        files={"file": ("hist.pdf", data, "application/pdf")},
    )
    assert r2.json() == {"job_id": job_id, "dedup": "true"}

    # Otro idioma destino → NO dedup (traducción distinta)
    r3 = client.post(
        "/api/translate",
        data={"mock": "true", "to": "fr"},
        files={"file": ("hist.pdf", data, "application/pdf")},
    )
    assert "dedup" not in r3.json()
    _wait_done(client, r3.json()["job_id"])

    # Borrar del historial: desaparece y el job queda 404
    assert client.delete(f"/api/jobs/{job_id}").json() == {"ok": True}
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    hist = client.get("/api/history").json()
    assert all(h["id"] != job_id for h in hist)


def test_missing_api_key_friendly_error(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post(
        "/api/translate",
        data={"to": "es"},  # sin mock y sin api_key
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 200
    data = _wait_done(client, r.json()["job_id"])
    assert data["status"] == "error", data
    assert "API key" in data["error"]


def test_translator_accepts_api_key_without_env(monkeypatch):
    from pdf_translator.translate import ClaudeTranslator

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    t = ClaudeTranslator(api_key="sk-ant-fake", verbose=False)
    assert t.model  # el cliente se construye con la key explícita sin explotar
