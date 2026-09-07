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
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .cli import LANG_NAMES, _parse_pages
from .lang_detect import detect_document_language
from .pipeline import translate_pdf
from .translate import PROVIDER_ENV, make_translator

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
        if meta and meta.get("status") == "done":
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
        return {"job_id": existing, "dedup": "true"}

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / "input.pdf"
    input_path.write_bytes(data)
    try:
        doc = fitz.open(str(input_path))
        page_count = doc.page_count
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
        }
    opts = {
        "provider": provider,
        "model": model.strip(),
        "source": source,
        "source_name": source_name,
        "to": to,
        "glossary": glossary_terms,
        "pages": page_indexes,
        "api_key": api_key.strip(),
    }
    threading.Thread(target=_run, args=(job_id, input_path, output_path, opts), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = _snapshot(job_id)
    return {k: v for k, v in job.items() if k not in ("output", "created")}


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
