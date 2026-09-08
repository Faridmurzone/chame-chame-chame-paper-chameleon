"""Servidor web con UI para traducir PDFs desde el navegador.

    pdf-translate-web [--host 127.0.0.1] [--port 8000]

o directamente con uvicorn: uvicorn pdf_translator.web:app

API:
    POST /api/translate          — sube el PDF y encola el job (multipart/form-data);
                                   si el mismo archivo ya fue traducido, reutiliza el job
    GET  /api/jobs/{id}          — estado y progreso del job
    GET  /api/jobs/{id}/download — PDF traducido
    GET  /api/jobs/{id}/original — PDF original
    GET  /api/history            — historial de traducciones completadas
    DELETE /api/jobs/{id}        — borra un job del historial
    POST /api/jobs/{id}/retry    — reintenta un job con error (nueva key/proveedor/modelo)
    POST /api/jobs/{id}/share    — comparte la traducción con la comunidad
    DELETE /api/jobs/{id}/share  — deja de compartirla
    PATCH /api/jobs/{id}/meta    — edita título, autores y keywords
    GET  /api/community?q=&to=   — biblioteca comunitaria con buscador
    GET  /api/meta               — idiomas, proveedores y catálogo de modelos

Los PDFs y metadatos persisten en ~/.pdf-translator/jobs (configurable con
PDF_TRANSLATE_DATA_DIR); los jobs completados no expiran, se borran a mano.
"""

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import fitz
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .cli import LANG_NAMES, _parse_pages
from .lang_detect import detect_document_language
from .pipeline import translate_pdf
from .translate import PROVIDER_ENV, extract_paper_metadata, make_translator

STATIC_DIR = Path(__file__).parent / "static"
DATA_DIR = Path(os.environ.get("PDF_TRANSLATE_DATA_DIR") or Path.home() / ".pdf-translator")
JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
META_FILE = "meta.json"
MAX_UPLOAD = 200 * 1024 * 1024  # 200 MB
JOB_TTL = 48 * 3600  # los jobs INCOMPLETOS se borran tras 48 h; los done persisten

PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google Gemini",
    "deepseek": "DeepSeek",
}

# Modelos curados por proveedor, aptos para traducción de papers con salida JSON.
# El primero de cada lista es el default del proveedor.
MODEL_CATALOG: dict[str, list[dict[str, str]]] = {
    "anthropic": [
        {"id": "claude-opus-5", "label": "Claude Opus 5 — máxima calidad"},
        {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5 — balance"},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5 — rápido y económico"},
    ],
    "openai": [
        {"id": "gpt-5.1", "label": "GPT-5.1"},
        {"id": "gpt-5.1-mini", "label": "GPT-5.1 mini — económico"},
        {"id": "gpt-4.1", "label": "GPT-4.1 — largo contexto"},
    ],
    "google": [
        {"id": "gemini-3-pro-preview", "label": "Gemini 3 Pro (preview)"},
        {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash — rápido y económico"},
    ],
    "deepseek": [
        {"id": "deepseek-chat", "label": "DeepSeek V3 (chat)"},
        {"id": "deepseek-reasoner", "label": "DeepSeek R1 (reasoner)"},
    ],
}

app = FastAPI(title="Paper Chameleon", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def _update(job_id: str, **kw: Any) -> None:
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(kw)


def _snapshot(job_id: str) -> dict[str, Any]:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inexistente.")
        return dict(job)


def _save_meta(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        snapshot = dict(job) if job else None
    if snapshot is None:
        return
    try:
        (JOBS_DIR / job_id / META_FILE).write_text(
            json.dumps(snapshot), encoding="utf-8"
        )
    except OSError:
        pass


def _load_meta(job_id: str) -> dict[str, Any] | None:
    try:
        return json.loads((JOBS_DIR / job_id / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _hydrate_history() -> None:
    """Carga al arranque los jobs done del disco (historial persistente)."""
    try:
        entries = list(JOBS_DIR.iterdir())
    except OSError:
        return
    for path in entries:
        if not path.is_dir():
            continue
        meta = _load_meta(path.name)
        if meta and meta.get("status") in ("done", "error"):
            with _lock:
                _jobs.setdefault(path.name, meta)


def _cleanup_old_jobs() -> None:
    now = time.time()
    try:
        entries = list(JOBS_DIR.iterdir())
    except OSError:
        entries = []
    for path in entries:
        if not path.is_dir():
            continue
        meta = _load_meta(path.name)
        if meta and meta.get("status") == "done":
            continue  # completado: persiste hasta borrarse a mano
        try:
            if now - path.stat().st_mtime > JOB_TTL:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
    with _lock:
        for jid in [
            j for j, job in _jobs.items()
            if job.get("status") != "done" and now - job.get("created", 0) > JOB_TTL
        ]:
            _jobs.pop(jid, None)


def _page1_title(doc: "fitz.Document") -> str:
    """Texto más grande de la página 1: fallback de título sin metadata."""
    try:
        if not doc.page_count:
            return ""
        best, best_size = "", 0.0
        for block in doc[0].get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = "".join(s.get("text", "") for s in spans).strip()
                size = max((s.get("size", 0) for s in spans), default=0.0)
                if text and len(text) >= 12 and size > best_size:
                    best, best_size = text, size
        return best.strip()
    except Exception:
        return ""


def _paper_meta_from_doc(doc: "fitz.Document", fallback: str) -> tuple[str, str]:
    """(título, autores): metadata del PDF → texto grande de pág. 1 → fallback."""
    md = doc.metadata or {}
    title = (md.get("title") or "").strip()
    authors = (md.get("author") or "").strip()
    if not title:
        title = _page1_title(doc)
    return title or fallback, authors


def _page1_text(job_id: str, limit: int = 6000) -> str:
    try:
        doc = fitz.open(str(JOBS_DIR / job_id / "input.pdf"))
        text = doc[0].get_text() if doc.page_count else ""
        doc.close()
        return text[:limit]
    except Exception:
        return ""


def _llm_paper_meta(job: dict[str, Any], api_key: str) -> dict[str, str] | None:
    """Título/autores/keywords con una llamada LLM. None si falla (hay fallback)."""
    try:
        return extract_paper_metadata(
            provider=job.get("provider", "anthropic"),
            model=job.get("model", "") or "",
            api_key=api_key,
            text=_page1_text(job["id"]),
        )
    except Exception:
        return None


def _run(
    job_id: str,
    input_path: Path,
    output_path: Path,
    opts: dict[str, Any],
) -> None:
    def progress(stage: str, current: int, total: int) -> None:
        _update(job_id, stage=stage, current=current, total=total)

    try:
        provider = opts["provider"]
        env_var = PROVIDER_ENV[provider]
        # Prioridad: key de la UI > variable de entorno. Nunca se guarda en el job.
        api_key = (opts.get("api_key") or "").strip() or os.environ.get(env_var)
        if not api_key:
            raise RuntimeError(
                f"Falta la API key de {PROVIDER_LABELS[provider]}: ingresala en la UI "
                f"(o exportá {env_var})."
            )
        translator = make_translator(
            provider=provider,
            model=opts["model"] or None,
            source_lang=opts["source_name"],
            target_lang=LANG_NAMES.get(opts["to"], opts["to"]),
            glossary=opts["glossary"],
            api_key=api_key,
        )
        stats = translate_pdf(
            str(input_path),
            str(output_path),
            translator,
            pages=opts["pages"],
            verbose=False,
            progress=progress,
        )
        _update(
            job_id,
            status="done",
            stage="done",
            output=str(output_path),
            message=(
                f"{stats['translated']} bloques traducidos · "
                f"{stats['skipped']} preservados (fórmulas, código, números, imágenes)"
            ),
        )
        # Compartido: título/autores/keywords finos con una llamada LLM.
        # Si falla, quedan los extraídos heurísticamente al subir el PDF.
        if opts.get("share"):
            llm = _llm_paper_meta(_snapshot(job_id), opts.get("api_key", ""))
            if llm and any(llm.values()):
                _update(job_id, **{k: v for k, v in llm.items() if v})
        _save_meta(job_id)
    except Exception as e:  # noqa: BLE001 — el error viaja al frontend
        _update(job_id, status="error", error=str(e))
        _save_meta(job_id)


@app.post("/api/translate")
async def translate(
    file: UploadFile = File(...),
    to: str = Form("es"),
    source: str = Form("auto"),
    provider: str = Form("anthropic"),
    model: str = Form(""),
    pages: str = Form(""),
    glossary: str = Form(""),
    share: bool = Form(False),
    api_key: str = Form(""),
) -> dict[str, str]:
    _cleanup_old_jobs()
    name = file.filename or ""
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "El archivo debe ser un PDF (.pdf).")
    data = await file.read()
    if not data:
        raise HTTPException(400, "El archivo está vacío.")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"El PDF supera el límite de {MAX_UPLOAD // (1024 * 1024)} MB.")
    provider = provider if provider in MODEL_CATALOG else "anthropic"
    try:
        page_indexes = _parse_pages(pages) if pages.strip() else None
    except (ValueError, TypeError):
        raise HTTPException(400, "Páginas inválidas: usá el formato '1-3,7'.") from None
    glossary_terms = [ln.strip() for ln in glossary.splitlines() if ln.strip()]

    # Dedup: si este mismo archivo ya se tradujo al mismo destino, reutilizamos.
    # Los jobs demo viejos (mock) nunca se sirven como traducción real.
    file_hash = hashlib.sha256(data).hexdigest()
    _hydrate_history()
    with _lock:
        existing = next(
            (j["id"] for j in _jobs.values()
             if j.get("hash") == file_hash and j.get("status") == "done"
             and j.get("to") == to and not j.get("mock")),
            None,
        )
    if existing:
        if share:
            _apply_share(existing, {})
        return {"job_id": existing, "dedup": "true"}

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / "input.pdf"
    input_path.write_bytes(data)
    stem = Path(name).stem.replace("-", " ").replace("_", " ")
    title, authors = stem, ""
    try:
        doc = fitz.open(str(input_path))
        page_count = doc.page_count
        # Título/autores para la estantería: metadata del PDF o el texto más
        # grande de la página 1; si nada, el nombre de archivo humanizado.
        title, authors = _paper_meta_from_doc(doc, stem)
        doc.close()
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "El PDF no se pudo abrir: parece corrupto o ilegible.") from None

    # Resolución del idioma origen: "auto" detecta desde el PDF
    if source == "auto":
        source_name = detect_document_language(str(input_path))
    else:
        source_name = LANG_NAMES.get(source, source)

    out_name = f"{Path(name).stem}.{to}.pdf"
    output_path = job_dir / out_name
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "filename": name,
            "status": "running",
            "stage": "extract",
            "current": 0,
            "total": 0,
            "message": "",
            "error": None,
            "out_name": out_name,
            "output": None,
            "pages": page_count,
            "source_name": source_name,
            "to": to,
            "provider": provider,
            "model": model.strip(),
            "hash": file_hash,
            "created": time.time(),
            "shared": share,
            "shared_at": time.time() if share else 0,
            "title": title,
            "authors": authors,
            "keywords": "",
            # datos para reintentar tras un error (p.ej. key rechazada)
            "retry_opts": {"pages": page_indexes, "glossary": glossary_terms},
        }
    opts = {
        "provider": provider,
        "model": model.strip(),
        "source": source,
        "source_name": source_name,
        "to": to,
        "glossary": glossary_terms,
        "pages": page_indexes,
        "share": share,
        "api_key": api_key.strip(),
    }
    threading.Thread(target=_run, args=(job_id, input_path, output_path, opts), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = _snapshot(job_id)
    return {k: v for k, v in job.items() if k not in ("output", "created", "retry_opts")}


@app.get("/api/jobs/{job_id}/download")
def job_download(job_id: str) -> FileResponse:
    job = _snapshot(job_id)
    if job["status"] != "done" or not job["output"]:
        raise HTTPException(404, "El PDF traducido aún no está disponible.")
    path = Path(job["output"])
    if not path.exists():
        raise HTTPException(404, "El archivo ya no existe (expiró).")
    return FileResponse(
        path, media_type="application/pdf", filename=job["out_name"],
        content_disposition_type="inline",
    )


@app.get("/api/jobs/{job_id}/original")
def job_original(job_id: str) -> FileResponse:
    job = _snapshot(job_id)
    path = JOBS_DIR / job_id / "input.pdf"
    if not path.exists():
        raise HTTPException(404, "El PDF original ya no existe (expiró).")
    return FileResponse(
        path, media_type="application/pdf", filename=job["filename"],
        content_disposition_type="inline",
    )


@app.get("/api/history")
def history() -> list[dict[str, Any]]:
    _hydrate_history()
    with _lock:
        done = [
            dict(j) for j in _jobs.values()
            if j.get("status") == "done" and not j.get("mock")
        ]
    items = [
        {
            "id": j["id"],
            "filename": j.get("filename", ""),
            "created": j.get("created", 0),
            "source_name": j.get("source_name", ""),
            "to": j.get("to", ""),
            "provider": j.get("provider", ""),
            "model": j.get("model", ""),
            "pages": j.get("pages", 0),
            "message": j.get("message", ""),
            "title": j.get("title", ""),
            "authors": j.get("authors", ""),
            "keywords": j.get("keywords", ""),
            "shared": bool(j.get("shared")),
        }
        for j in done
    ]
    items.sort(key=lambda x: x["created"], reverse=True)
    return items


@app.delete("/api/jobs/{job_id}")
def job_delete(job_id: str) -> dict[str, bool]:
    with _lock:
        _jobs.pop(job_id, None)
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
    return {"ok": True}


def _apply_share(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    job = _snapshot(job_id)
    if job["status"] != "done":
        raise HTTPException(400, "Solo se pueden compartir traducciones completas.")
    # Prioridad: datos editados por el usuario > LLM > heurística del PDF.
    title = (payload.get("title") or "").strip()
    authors = (payload.get("authors") or "").strip()
    keywords = (payload.get("keywords") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    if api_key and not (title and authors and keywords):
        llm = _llm_paper_meta(job, api_key) or {}
        title = title or llm.get("title", "")
        authors = authors or llm.get("authors", "")
        keywords = keywords or llm.get("keywords", "")
    if not title:
        try:
            doc = fitz.open(str(JOBS_DIR / job_id / "input.pdf"))
            auto_title, auto_authors = _paper_meta_from_doc(doc, job.get("filename", ""))
            doc.close()
        except Exception:
            auto_title, auto_authors = "", ""
        title = title or auto_title or job.get("title", "") or job.get("filename", "")
        authors = authors or auto_authors
    _update(
        job_id,
        shared=True,
        shared_at=job.get("shared_at") or time.time(),
        title=title,
        authors=authors,
        keywords=keywords,
    )
    _save_meta(job_id)
    return {"ok": True, "title": title, "authors": authors, "keywords": keywords}


@app.post("/api/jobs/{job_id}/retry")
def job_retry(job_id: str, payload: dict = Body(...)) -> dict[str, Any]:
    """Reintenta un job con error: nueva key/proveedor/modelo, mismos datos del paper."""
    job = _snapshot(job_id)
    if job.get("status") != "error":
        raise HTTPException(409, "Solo se pueden reintentar jobs con error.")
    input_path = JOBS_DIR / job_id / "input.pdf"
    if not input_path.exists():
        raise HTTPException(404, "El PDF original ya no existe (expiró).")
    provider = payload.get("provider") or job.get("provider") or "anthropic"
    provider = provider if provider in MODEL_CATALOG else "anthropic"
    model = (payload.get("model") or job.get("model") or "").strip()
    api_key = (payload.get("api_key") or "").strip() or os.environ.get(PROVIDER_ENV[provider])
    if not api_key:
        raise HTTPException(400, "Falta la API key para reintentar.")
    base = job.get("retry_opts") or {}
    opts = {
        "provider": provider,
        "model": model,
        "source_name": job.get("source_name", ""),
        "to": job.get("to", "es"),
        "glossary": base.get("glossary", []),
        "pages": base.get("pages") or None,
        "share": bool(job.get("shared")),
        "api_key": api_key,
    }
    out_name = f"{Path(job.get('filename') or 'paper').stem}.{job.get('to') or 'es'}.pdf"
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update({
                "status": "running",
                "stage": "extract",
                "current": 0,
                "total": 0,
                "message": "",
                "error": None,
                "provider": provider,
                "model": model,
                "out_name": out_name,
            })
    threading.Thread(
        target=_run,
        args=(job_id, input_path, JOBS_DIR / job_id / out_name, opts),
        daemon=True,
    ).start()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/share")
def job_share(job_id: str, payload: dict | None = Body(default=None)) -> dict[str, Any]:
    """Comparte una traducción terminada con la comunidad (payload opcional)."""
    return _apply_share(job_id, payload or {})


@app.patch("/api/jobs/{job_id}/meta")
def job_edit_meta(job_id: str, payload: dict = Body(...)) -> dict[str, Any]:
    """Edita título, autores y keywords del paper (independiente de compartir)."""
    _snapshot(job_id)
    fields: dict[str, str] = {}
    for key in ("title", "authors", "keywords"):
        if key in payload:
            val = str(payload[key]).strip()
            if val or key != "title":  # el título no puede quedar vacío
                fields[key] = val
    if not fields:
        raise HTTPException(400, "Nada para actualizar.")
    _update(job_id, **fields)
    _save_meta(job_id)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}/share")
def job_unshare(job_id: str) -> dict[str, bool]:
    _snapshot(job_id)
    _update(job_id, shared=False)
    _save_meta(job_id)
    return {"ok": True}


@app.get("/api/community")
def community(q: str = "", to: str = "") -> list[dict[str, Any]]:
    """Biblioteca comunitaria: papers compartidos con búsqueda por texto."""
    _hydrate_history()
    with _lock:
        shared = [
            dict(j) for j in _jobs.values()
            if j.get("status") == "done" and j.get("shared")
        ]
    terms = [t.lower() for t in q.split() if t.strip()]
    items = []
    for j in shared:
        if to and j.get("to") != to:
            continue
        hay = " ".join([
            j.get("title", ""), j.get("authors", ""),
            j.get("keywords", ""), j.get("filename", ""),
        ]).lower()
        if terms and not all(t in hay for t in terms):
            continue
        items.append({
            "id": j["id"],
            "title": j.get("title", ""),
            "authors": j.get("authors", ""),
            "keywords": j.get("keywords", ""),
            "filename": j.get("filename", ""),
            "created": j.get("created", 0),
            "shared_at": j.get("shared_at", 0),
            "source_name": j.get("source_name", ""),
            "to": j.get("to", ""),
            "provider": j.get("provider", ""),
            "model": j.get("model", ""),
            "pages": j.get("pages", 0),
        })
    items.sort(key=lambda x: x["shared_at"] or x["created"], reverse=True)
    return items


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    models = {
        provider: [
            {**m, "default": i == 0}
            for i, m in enumerate(catalog)
        ]
        for provider, catalog in MODEL_CATALOG.items()
    }
    return {
        "languages": LANG_NAMES,
        "providers": [{"id": p, "label": PROVIDER_LABELS[p]} for p in MODEL_CATALOG],
        "models": models,
    }


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="pdf-translate-web",
        description="Paper Chameleon: subí un PDF y descargá la traducción.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    import uvicorn

    print(f"Paper Chameleon → http://{args.host}:{args.port}", flush=True)
    print(f"historial y PDFs en {DATA_DIR}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
