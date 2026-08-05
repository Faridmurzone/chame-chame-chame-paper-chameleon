"""CLI: pdf-translate entrada.pdf [-o salida.pdf] [--to es] [--mock] ..."""

import argparse
import pathlib
import sys

LANG_NAMES = {
    "es": "Spanish",
    "en": "English",
    "pt": "Portuguese",
    "fr": "French",
    "de": "German",
    "it": "Italian",
}


def _parse_pages(spec: str) -> list[int]:
    """'1-3,7' -> [0, 1, 2, 6] (índices base 0)."""
    pages: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            pages.extend(range(int(a) - 1, int(b)))
        else:
            pages.append(int(part) - 1)
    return sorted(set(pages))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdf-translate",
        description="Traduce un PDF preservando layout, imágenes y fórmulas.",
    )
    parser.add_argument("input", help="PDF de entrada")
    parser.add_argument("-o", "--output", help="PDF de salida (default: <entrada>.<lang>.pdf)")
    parser.add_argument("--to", default="es", help="Idioma destino (default: es)")
    parser.add_argument("--source", default="en", help="Idioma origen (default: en)")
    parser.add_argument("--model", default=None, help="Modelo de Claude a usar")
    parser.add_argument("--glossary", help="Archivo con términos (uno por línea) a NO traducir")
    parser.add_argument("--pages", help="Páginas a traducir, ej. '1-3,7' (default: todas)")
    parser.add_argument(
        "--mock", action="store_true",
        help="No llama a la API: marca los textos con [ES] (para probar el layout)",
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args(argv)

    input_path = pathlib.Path(args.input)
    if not input_path.exists():
        parser.error(f"no existe el archivo: {input_path}")
    output = args.output or str(input_path.with_suffix(f".{args.to}.pdf"))

    if args.mock:
        from .translate import MockTranslator

        translator = MockTranslator()
    else:
        from .translate import DEFAULT_MODEL, ClaudeTranslator

        glossary = None
        if args.glossary:
            glossary = [
                line.strip()
                for line in pathlib.Path(args.glossary).read_text().splitlines()
                if line.strip()
            ]
        translator = ClaudeTranslator(
            model=args.model or DEFAULT_MODEL,
            source_lang=LANG_NAMES.get(args.source, args.source),
            target_lang=LANG_NAMES.get(args.to, args.to),
            glossary=glossary,
            verbose=not args.quiet,
        )

    from .pipeline import translate_pdf

    stats = translate_pdf(
        str(input_path),
        output,
        translator,
        pages=_parse_pages(args.pages) if args.pages else None,
        verbose=not args.quiet,
    )
    if not args.quiet:
        print(
            f"Listo: {output} ({stats['translated']} bloques traducidos, "
            f"{stats['skipped']} preservados)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
