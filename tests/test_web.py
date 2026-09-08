import hashlib
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


@pytest.fixture
def fake_api(client, monkeypatch):
    """Corre el pipeline real con un traductor falso: no toca ninguna API."""
    from pdf_translator.translate import MockTranslator
    import pdf_translator.web as web

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(web, "make_translator", lambda **kw: MockTranslator())
    monkeypatch.setattr(
        web, "extract_paper_metadata",
        lambda **kw: {"title": "Titulo LLM", "authors": "Autor LLM", "keywords": "llm, test"},
    )
    return client


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
    assert "Paper Chameleon" in r.text
    assert "api/translate" in r.text


def test_static_assets_served(client):
    for path in ("/static/logo.png", "/static/favicon-16.png", "/static/favicon-32.png"):
        r = client.get(path)
        assert r.status_code == 200


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


def test_translate_flow(fake_api):
    r = fake_api.post(
        "/api/translate",
        data={"to": "es"},
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    data = _wait_done(fake_api, job_id)
    assert data["status"] == "done", data
    assert data["stage"] == "done"
    assert "traducidos" in data["message"]

    dl = fake_api.get(f"/api/jobs/{job_id}/download")
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/pdf"
    doc = fitz.open(stream=dl.content, filetype="pdf")
    assert "[ES]" in doc[0].get_text()
    doc.close()


def test_job_status_hides_internal_fields(fake_api):
    r = fake_api.post(
        "/api/translate",
        data={},
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    job_id = r.json()["job_id"]
    data = _wait_done(fake_api, job_id)
    assert "output" not in data and "created" not in data
    # pages llega para el pager del visor
    assert data["pages"] == 1


def test_original_download(fake_api):
    r = fake_api.post(
        "/api/translate",
        data={},
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    job_id = r.json()["job_id"]
    _wait_done(fake_api, job_id)
    orig = fake_api.get(f"/api/jobs/{job_id}/original")
    assert orig.status_code == 200
    assert orig.headers["content-type"] == "application/pdf"
    assert "inline" in orig.headers["content-disposition"]
    doc = fitz.open(stream=orig.content, filetype="pdf")
    assert "agentic loop" in doc[0].get_text()
    doc.close()


def test_rejects_non_pdf(client):
    r = client.post(
        "/api/translate",
        data={},
        files={"file": ("x.txt", b"nope", "text/plain")},
    )
    assert r.status_code == 400


def test_rejects_corrupt_pdf(client):
    r = client.post(
        "/api/translate",
        data={},
        files={"file": ("x.pdf", b"esto no es un pdf", "application/pdf")},
    )
    assert r.status_code == 400


def test_rejects_bad_pages_spec(client):
    r = client.post(
        "/api/translate",
        data={"pages": "abc"},
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


def test_history_dedup_and_delete(fake_api):
    data = _unique_pdf_bytes(f"{time.time()}")
    r1 = fake_api.post(
        "/api/translate",
        data={"to": "es"},
        files={"file": ("hist.pdf", data, "application/pdf")},
    )
    job_id = r1.json()["job_id"]
    _wait_done(fake_api, job_id)

    # El historial lo lista
    hist = fake_api.get("/api/history").json()
    entry = next((h for h in hist if h["id"] == job_id), None)
    assert entry is not None
    assert entry["filename"] == "hist.pdf"
    assert entry["to"] == "es" and entry["pages"] == 1

    # Re-subir el mismo archivo → dedup: mismo job, sin re-traducir
    r2 = fake_api.post(
        "/api/translate",
        data={"to": "es"},
        files={"file": ("hist.pdf", data, "application/pdf")},
    )
    assert r2.json() == {"job_id": job_id, "dedup": "true"}

    # Otro idioma destino → NO dedup (traducción distinta)
    r3 = fake_api.post(
        "/api/translate",
        data={"to": "fr"},
        files={"file": ("hist.pdf", data, "application/pdf")},
    )
    assert "dedup" not in r3.json()
    _wait_done(fake_api, r3.json()["job_id"])

    # Borrar del historial: desaparece y el job queda 404
    assert fake_api.delete(f"/api/jobs/{job_id}").json() == {"ok": True}
    assert fake_api.get(f"/api/jobs/{job_id}").status_code == 404
    hist = fake_api.get("/api/history").json()
    assert all(h["id"] != job_id for h in hist)


def test_dedup_ignores_old_demo_jobs(client, monkeypatch):
    """Un job demo (mock) viejo no se lista ni se sirve como traducción real."""
    import pdf_translator.web as web

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    payload = _unique_pdf_bytes(f"demo-legacy-{time.time()}")
    with web._lock:
        web._jobs["deadbeef"] = {
            "id": "deadbeef", "filename": "viejo.pdf", "status": "done",
            "mock": True, "to": "es",
            "hash": hashlib.sha256(payload).hexdigest(),
            "created": time.time(),
        }
    hist = client.get("/api/history").json()
    assert all(h["id"] != "deadbeef" for h in hist)
    r = client.post(
        "/api/translate", data={"to": "es"},
        files={"file": ("nuevo.pdf", payload, "application/pdf")},
    )
    assert "dedup" not in r.json()
    with web._lock:
        web._jobs.pop("deadbeef", None)


def test_missing_api_key_friendly_error(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post(
        "/api/translate",
        data={"to": "es"},  # sin api_key en env ni en el form
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


def _done_job(fake_api, tag: str, share: bool = False):
    payload = _unique_pdf_bytes(tag)
    r = fake_api.post(
        "/api/translate",
        data={"to": "es", "share": "true"} if share else {"to": "es"},
        files={"file": ("shelf.pdf", payload, "application/pdf")},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    data = _wait_done(fake_api, job_id)
    assert data["status"] == "done", data
    return job_id


def test_share_and_community_search(fake_api):
    job_id = _done_job(fake_api, f"comunitario-{time.time()}", share=True)
    com = fake_api.get("/api/community").json()
    entry = next((c for c in com if c["id"] == job_id), None)
    assert entry is not None
    # título del paper extraído, no el nombre del archivo
    assert entry["title"] and entry["title"] != "shelf.pdf"
    assert entry["to"] == "es" and entry["pages"] == 1

    # buscador: por término del título y sin resultados
    term = entry["title"].split()[0].lower()
    assert any(c["id"] == job_id for c in fake_api.get("/api/community", params={"q": term}).json())
    assert fake_api.get("/api/community", params={"q": "zzzinexistente"}).json() == []

    # editar título y keywords (PATCH) → se refleja en la comunidad y el buscador
    new_title = f"Titulo Editado {time.time()}"
    r = fake_api.patch(
        f"/api/jobs/{job_id}/meta",
        json={"title": new_title, "keywords": "agents, llm"},
    )
    assert r.json()["ok"] is True
    entry = next(c for c in fake_api.get("/api/community").json() if c["id"] == job_id)
    assert entry["title"] == new_title
    assert any(c["id"] == job_id for c in fake_api.get("/api/community", params={"q": "llm"}).json())


def test_unshare_and_history_fields(fake_api):
    job_id = _done_job(fake_api, f"unshare-{time.time()}", share=True)
    entry = next(h for h in fake_api.get("/api/history").json() if h["id"] == job_id)
    assert entry["shared"] is True and entry["title"]
    assert fake_api.delete(f"/api/jobs/{job_id}/share").json() == {"ok": True}
    entry = next(h for h in fake_api.get("/api/history").json() if h["id"] == job_id)
    assert entry["shared"] is False
    assert all(c["id"] != job_id for c in fake_api.get("/api/community").json())


def test_dedup_with_share_propagates(fake_api):
    payload = _unique_pdf_bytes(f"dedup-share-{time.time()}")
    r1 = fake_api.post(
        "/api/translate", data={"to": "es"},
        files={"file": ("a.pdf", payload, "application/pdf")},
    )
    job_id = r1.json()["job_id"]
    _wait_done(fake_api, job_id)
    r2 = fake_api.post(
        "/api/translate",
        data={"to": "es", "share": "true"},
        files={"file": ("b.pdf", payload, "application/pdf")},
    )
    assert r2.json() == {"job_id": job_id, "dedup": "true"}
    assert any(c["id"] == job_id for c in fake_api.get("/api/community").json())


def test_share_metadata_from_llm(fake_api):
    # Compartir con checkbox → al terminar, el LLM completa título/autores/keywords
    job_id = _done_job(fake_api, f"llm-meta-{time.time()}", share=True)
    entry = next(h for h in fake_api.get("/api/history").json() if h["id"] == job_id)
    assert entry["title"] == "Titulo LLM"
    assert entry["authors"] == "Autor LLM"
    assert entry["keywords"] == "llm, test"
    com = next(c for c in fake_api.get("/api/community").json() if c["id"] == job_id)
    assert com["title"] == "Titulo LLM"


def test_share_requires_done(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post(
        "/api/translate", data={"to": "es"},
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    job_id = r.json()["job_id"]
    assert _wait_done(client, job_id)["status"] == "error"
    assert client.post(f"/api/jobs/{job_id}/share", json={}).status_code == 400


def test_retry_with_new_credentials(client, monkeypatch):
    """Key rechazada → error → retry con nueva key/proveedor/modelo → done."""
    from pdf_translator.translate import MockTranslator
    import pdf_translator.web as web

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post(
        "/api/translate",
        data={"to": "es", "pages": "1", "glossary": "transformer"},
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    job_id = r.json()["job_id"]
    assert _wait_done(client, job_id)["status"] == "error"

    monkeypatch.setattr(web, "make_translator", lambda **kw: MockTranslator())
    r = client.post(
        f"/api/jobs/{job_id}/retry",
        json={"provider": "anthropic", "model": "claude-haiku-4-5", "api_key": "sk-nueva"},
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    data = _wait_done(client, job_id)
    assert data["status"] == "done", data
    assert data["provider"] == "anthropic"
    assert data["model"] == "claude-haiku-4-5"


def test_retry_guardrails(client, monkeypatch):
    from pdf_translator.translate import MockTranslator
    import pdf_translator.web as web

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # job inexistente
    assert client.post("/api/jobs/zzzz/retry", json={"api_key": "k"}).status_code == 404
    # sin key en el pedido ni en el entorno → 400
    r = client.post(
        "/api/translate", data={"to": "es"},
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    job_id = r.json()["job_id"]
    assert _wait_done(client, job_id)["status"] == "error"
    assert client.post(f"/api/jobs/{job_id}/retry", json={}).status_code == 400

    # con nueva key → done; un job done ya no se puede reintentar (409)
    monkeypatch.setattr(web, "make_translator", lambda **kw: MockTranslator())
    assert client.post(
        f"/api/jobs/{job_id}/retry",
        json={"provider": "anthropic", "api_key": "sk-nueva"},
    ).status_code == 200
    assert _wait_done(client, job_id)["status"] == "done"
    assert client.post(f"/api/jobs/{job_id}/retry", json={"api_key": "k"}).status_code == 409
