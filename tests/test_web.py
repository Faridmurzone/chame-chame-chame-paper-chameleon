import io
import time

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
    assert data["default_model"]


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
