FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY pdf_translator ./pdf_translator

RUN pip install --no-cache-dir ".[web,providers]"

# Hub: corre como usuario no-root con los datos en /data
RUN useradd -m hub && mkdir -p /data && chown hub:hub /data
USER hub

ENV PDF_TRANSLATE_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8000

CMD ["uvicorn", "pdf_translator.web:app", "--host", "0.0.0.0", "--port", "8000"]
