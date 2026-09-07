"""Servidor web con UI para traducir PDFs desde el navegador.

    pdf-translate-web [--host 127.0.0.1] [--port 8000]

o directamente con uvicorn: uvicorn pdf_translator.web:app

API:
    POST /api/translate          — sube el PDF y encola el job (multipart/form-data)
    GET  /api/jobs/{id}          — estado y progreso del job
    GET  /api/jobs/{id}/download — PDF traducido
    GET  /api/meta               — idiomas soportados y modelo default
"""

import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import fitz
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .cli import LANG_NAMES, _parse_pages
from .pipeline import translate_pdf
from .translate import DEFAULT_MODEL, ClaudeTranslator, MockTranslator

STATIC_DIR = Path(__file__).parent / "static"
JOBS_DIR = Path(
    os.environ.get("PDF_TRANSLATE_JOBS_DIR") or Path(tempfile.gettempdir()) / "pdf-translate-jobs"
)
MAX_UPLOAD = 200 * 1024 * 1024  # 200 MB
JOB_TTL = 48 * 3600  # los trabajos y sus archivos viven 48 h

app = FastAPI(title="pdf-translator", docs_url=None, redoc_url=None)

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


def _cleanup_old_jobs() -> None:
    now = time.time()
    try:
        entries = list(JOBS_DIR.iterdir())
    except OSError:
        entries = []
    for path in entries:
        try:
            if now - path.stat().st_mtime > JOB_TTL:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
    with _lock:
        for jid in [j for j, job in _jobs.items() if now - job["created"] > JOB_TTL]:
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
        if opts["mock"]:
            translator = MockTranslator()
        else:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError(
                    "Falta la variable de entorno ANTHROPIC_API_KEY "
                    "(o usá el modo demo sin API)."
                )
            translator = ClaudeTranslator(
                model=opts["model"] or DEFAULT_MODEL,
                source_lang=LANG_NAMES.get(opts["source"], opts["source"]),
                target_lang=LANG_NAMES.get(opts["to"], opts["to"]),
                glossary=opts["glossary"],
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
    except Exception as e:  # noqa: BLE001 — el error viaja al frontend
        _update(job_id, status="error", error=str(e))


@app.post("/api/translate")
async def translate(
    file: UploadFile = File(...),
    to: str = Form("es"),
    source: str = Form("en"),
    model: str = Form(""),
    pages: str = Form(""),
    glossary: str = Form(""),
    mock: bool = Form(False),
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
    try:
        page_indexes = _parse_pages(pages) if pages.strip() else None
    except (ValueError, TypeError):
        raise HTTPException(400, "Páginas inválidas: usá el formato '1-3,7'.") from None
    glossary_terms = [ln.strip() for ln in glossary.splitlines() if ln.strip()]

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / "input.pdf"
    input_path.write_bytes(data)
    try:
        doc = fitz.open(str(input_path))
        doc.page_count
        doc.close()
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "El PDF no se pudo abrir: parece corrupto o ilegible.") from None

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
            "created": time.time(),
        }
    opts = {
        "mock": mock,
        "model": model.strip(),
        "source": source,
        "to": to,
        "glossary": glossary_terms,
        "pages": page_indexes,
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
    return FileResponse(path, media_type="application/pdf", filename=job["out_name"])


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    return {"languages": LANG_NAMES, "default_model": DEFAULT_MODEL}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="pdf-translate-web",
        description="UI web para pdf-translator: subí un PDF y descargá la traducción.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    import uvicorn

    print(f"pdf-translator web → http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
