"""Web UI: subí un PDF, elegí idioma, mirá el progreso y descargá la traducción.

Uso:  pdf-translate-web  (sirve en http://127.0.0.1:8484)
"""

import os
import pathlib
import tempfile
import threading
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from .cli import LANG_NAMES, TESSERACT_LANGS
from .pipeline import translate_pdf
from .translate import DEFAULT_MODEL, ClaudeTranslator, MockTranslator

app = FastAPI(title="pdf-translator")

STATIC_DIR = pathlib.Path(__file__).parent / "static"

# Jobs en memoria: es una herramienta local, no un servicio multiusuario
JOBS: dict[str, dict] = {}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    to: str = Form("es"),
    source: str = Form("en"),
    mock: bool = Form(False),
    api_key: str = Form(""),
    glossary: str = Form(""),
):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Subí un archivo .pdf")
    if not mock and not api_key and not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(
            400,
            "Falta la API key de Anthropic: pasala en el formulario o definí "
            "ANTHROPIC_API_KEY en el servidor. También podés probar el modo mock.",
        )

    job_id = uuid.uuid4().hex[:12]
    workdir = pathlib.Path(tempfile.mkdtemp(prefix=f"pdftr-{job_id}-"))
    input_path = workdir / "input.pdf"
    input_path.write_bytes(await file.read())
    stem = pathlib.Path(file.filename).stem
    output_path = workdir / f"{stem}.{to}.pdf"

    job = {
        "status": "en cola",
        "stage": "",
        "done": 0,
        "total": 0,
        "error": None,
        "stats": None,
        "output": str(output_path),
        "filename": output_path.name,
    }
    JOBS[job_id] = job

    args = (job, str(input_path), str(output_path), to, source, mock, api_key, glossary)
    threading.Thread(target=_run_job, args=args, daemon=True).start()
    return {"id": job_id}


def _run_job(job, input_path, output_path, to, source, mock, api_key, glossary):
    def on_progress(stage: str, done: int = 0, total: int = 0) -> None:
        job["stage"], job["done"], job["total"] = stage, done, total

    try:
        job["status"] = "procesando"
        if mock:
            translator = MockTranslator()
        else:
            translator = ClaudeTranslator(
                model=DEFAULT_MODEL,
                source_lang=LANG_NAMES.get(source, source),
                target_lang=LANG_NAMES.get(to, to),
                glossary=[t.strip() for t in glossary.splitlines() if t.strip()] or None,
                api_key=api_key or None,
                verbose=False,
            )
        stats = translate_pdf(
            input_path,
            output_path,
            translator,
            ocr_language=TESSERACT_LANGS.get(source, "eng"),
            verbose=False,
            on_progress=on_progress,
        )
        job["stats"] = stats
        job["status"] = "listo"
    except Exception as e:  # el error viaja a la UI
        job["status"] = "error"
        job["error"] = str(e)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job inexistente")
    return {k: v for k, v in job.items() if k != "output"}


@app.get("/api/jobs/{job_id}/download")
def job_download(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job inexistente")
    if job["status"] != "listo":
        raise HTTPException(409, "El job todavía no terminó")
    return FileResponse(job["output"], filename=job["filename"], media_type="application/pdf")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8484)


if __name__ == "__main__":
    main()
