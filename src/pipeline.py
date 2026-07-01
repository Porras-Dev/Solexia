"""
Complete Solar Agent pipeline.

Orchestrates all modules in order:
  1. scraper.py         →  data/companies.json
  2. scorer.py          →  data/qualified_leads.json
  3. email_generator.py →  data/generated_emails.json
  4. solar_calculator.py→  data/leads_with_solar_data.json
  5. pdf_generator.py   →  data/proposals/

Usage:
    python pipeline.py                      # full pipeline
    python pipeline.py --desde 3            # resume from step 3
    python pipeline.py --solo scoring       # run only one step
    python pipeline.py --dry-run            # show plan without executing
    python pipeline.py --force              # no confirmation prompt
    python pipeline.py --estado             # show current file status
"""

import sys
import os
import json
import time
import subprocess
import argparse
from pathlib import Path
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent     # = src/
DATA_DIR = BASE_DIR.parent / "data"  # = project root/data/

# ─── Pipeline steps ──────────────────────────────────────────────────────────

PASOS = [
    {
        "num":         1,
        "nombre":      "scraper",
        "descripcion": "Company scraping from Google Maps",
        "script":      "scraper.py",
        "args_extra":  [],
        "output":      "companies.json",
        "requiere":    [],
        "nota":        "Requires GOOGLE_MAPS_API_KEY in .env",
    },
    {
        "num":         2,
        "nombre":      "scoring",
        "descripcion": "Solar potential scoring with Claude API",
        "script":      "scorer.py",
        "args_extra":  [],
        "output":      "qualified_leads.json",
        "requiere":    ["companies.json"],
    },
    {
        "num":         3,
        "nombre":      "emails",
        "descripcion": "Personalised email generation",
        "script":      "email_generator.py",
        "args_extra":  [],
        "output":      "generated_emails.json",
        "requiere":    ["qualified_leads.json"],
        "nota":        "",
    },
    {
        "num":         4,
        "nombre":      "pvgis",
        "descripcion": "Real solar production calculation (PVGIS)",
        "script":      "solar_calculator.py",
        "args_extra":  [],
        "output":      "leads_with_solar_data.json",
        "requiere":    ["qualified_leads.json"],
        "nota":        "Public API, ~5-10 min for 180 leads",
    },
    {
        "num":         5,
        "nombre":      "pdfs",
        "descripcion": "PDF proposal generation (ReportLab)",
        "script":      "pdf_generator.py",
        "args_extra":  [],
        "output":      "proposals/",
        "requiere":    ["leads_with_solar_data.json"],
        "nota":        "Requires: pip install reportlab",
    },
]

# ANSI colours (disabled if terminal doesn't support them)
def _color(texto: str, codigo: str) -> str:
    if sys.stdout.isatty() and os.name != "nt":
        return f"\033[{codigo}m{texto}\033[0m"
    return texto

VERDE    = lambda t: _color(t, "32")
ROJO     = lambda t: _color(t, "31")
AMARILLO = lambda t: _color(t, "33")
NEGRITA  = lambda t: _color(t, "1")
GRIS     = lambda t: _color(t, "90")


# ─── File status ──────────────────────────────────────────────────────────────

def _estado_archivo(nombre: str) -> dict:
    if nombre.endswith("/"):
        ruta = DATA_DIR / nombre.rstrip("/")
        if not ruta.exists():
            return {"existe": False, "conteo": 0, "mtime": None}
        pdfs = list(ruta.glob("*.pdf"))
        mtime = max((p.stat().st_mtime for p in pdfs), default=None)
        return {"existe": True, "conteo": len(pdfs), "mtime": mtime, "tipo": "dir"}

    ruta = DATA_DIR / nombre
    if not ruta.exists():
        return {"existe": False, "conteo": 0, "mtime": None}

    mtime = ruta.stat().st_mtime
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        conteo = len(datos) if isinstance(datos, list) else 1
    except Exception:
        conteo = -1

    return {"existe": True, "conteo": conteo, "mtime": mtime, "tipo": "json"}


def mostrar_estado():
    print(f"\n{'─'*62}")
    print(f"  CURRENT PIPELINE STATUS")
    print(f"{'─'*62}")
    for paso in PASOS:
        st = _estado_archivo(paso["output"])
        if st["existe"]:
            mtime_str = datetime.fromtimestamp(st["mtime"]).strftime("%d/%m %H:%M") if st["mtime"] else "?"
            conteo_str = f"{st['conteo']} items" if paso["output"].endswith("/") else f"{st['conteo']} records"
            print(f"  {VERDE('✓')} Step {paso['num']}: {paso['nombre']:<12} {paso['output']:<30} {conteo_str}  ({mtime_str})")
        else:
            print(f"  {GRIS('○')} Step {paso['num']}: {paso['nombre']:<12} {GRIS(paso['output'])}")
    print(f"{'─'*62}\n")


# ─── Prerequisite validation ─────────────────────────────────────────────────

def validar_requisitos(pasos_a_ejecutar: list[dict]) -> list[str]:
    errores = []
    for paso in pasos_a_ejecutar:
        for req in paso["requiere"]:
            if not (DATA_DIR / req).exists():
                errores.append(f"Step {paso['num']} ({paso['nombre']}) requires '{req}' which does not exist")
        script = BASE_DIR / paso["script"]
        if not script.exists():
            errores.append(f"Script not found: {paso['script']}")
    return errores


# ─── Step execution ──────────────────────────────────────────────────────────

def ejecutar_paso(paso: dict, verbose: bool = True) -> tuple[bool, float]:
    """
    Runs the step's script as a subprocess and streams its output.
    Returns (success, elapsed_seconds).
    """
    script = BASE_DIR / paso["script"]
    cmd    = [sys.executable, str(script)] + paso["args_extra"]

    t_inicio = time.time()
    ok = True

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(BASE_DIR),
        )

        prefijo = GRIS(f"  [{paso['nombre']}] ")
        for linea in proc.stdout:
            if verbose:
                print(prefijo + linea, end="", flush=True)

        proc.wait()
        ok = (proc.returncode == 0)

    except KeyboardInterrupt:
        proc.terminate()
        print(f"\n  ⚠ Step {paso['num']} interrupted by user")
        ok = False
    except Exception as exc:
        print(f"\n  ✗ Error launching {paso['script']}: {exc}")
        ok = False

    t_total = time.time() - t_inicio
    return ok, t_total


# ─── Plan display ─────────────────────────────────────────────────────────────

def mostrar_plan(pasos: list[dict]):
    print(f"\n{'═'*62}")
    print(f"  {'SOLAR AGENT — PIPELINE':^58}")
    print(f"{'═'*62}")
    for paso in pasos:
        st = _estado_archivo(paso["output"])
        estado_ico = VERDE("✓") if st["existe"] else GRIS("○")
        print(f"  {estado_ico} Step {paso['num']}/5  {paso['nombre']:<12} →  {paso['output']}")
        if paso.get("nota"):
            print(f"            {GRIS(paso['nota'])}")
    print(f"{'─'*62}\n")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Full Solar Agent pipeline"
    )
    parser.add_argument("--desde",   type=int, default=1,
                        help="Step number to start from (1-5)")
    parser.add_argument("--solo",    type=str, default="",
                        help="Name of the step to run alone (scraper|scoring|emails|pvgis|pdfs)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show plan without executing")
    parser.add_argument("--force",   action="store_true",
                        help="No confirmation prompt")
    parser.add_argument("--estado",  action="store_true",
                        help="Show current status and exit")
    parser.add_argument("--silencioso", action="store_true",
                        help="Do not print subprocess output")
    args = parser.parse_args()

    if args.estado:
        mostrar_estado()
        return

    # Select steps to run
    if args.solo:
        pasos = [p for p in PASOS if p["nombre"] == args.solo.lower()]
        if not pasos:
            nombres = [p["nombre"] for p in PASOS]
            print(f"⚠ Unknown step: '{args.solo}'. Available: {nombres}")
            sys.exit(1)
    else:
        pasos = [p for p in PASOS if p["num"] >= args.desde]

    if not pasos:
        print("No steps to run.")
        return

    # Show plan
    mostrar_plan(PASOS)

    if args.solo:
        print(f"  Mode: run only '{args.solo}'")
    elif args.desde > 1:
        print(f"  Mode: start from step {args.desde}")

    print(f"  Steps to run: {[p['nombre'] for p in pasos]}\n")

    # Validate prerequisites
    errores = validar_requisitos(pasos)
    if errores:
        print(f"{ROJO('✗ Prerequisites not met:')}")
        for e in errores:
            print(f"  • {e}")
        print("\n  Run previous steps first or use --desde N")
        sys.exit(1)

    if args.dry_run:
        print(f"{AMARILLO('  [DRY-RUN] Nothing will be executed.')}")
        for paso in pasos:
            print(f"  python {paso['script']} {' '.join(paso['args_extra'])}")
        return

    if not args.force:
        resp = input(f"Run {len(pasos)} step(s)? [y/N] ").strip().lower()
        if resp != "y":
            print("Cancelled.")
            return

    # Execute
    print()
    resultados: list[dict] = []
    t_pipeline = time.time()

    for paso in pasos:
        ts     = datetime.now().strftime("%H:%M:%S")
        titulo = f"[{ts}] ▶ Step {paso['num']}/5: {paso['descripcion']}"
        print(f"\n{NEGRITA(titulo)}")
        print(f"  Script: {paso['script']}  →  {paso['output']}")
        if paso.get("nota"):
            print(f"  Note: {AMARILLO(paso['nota'])}")
        print(f"{'─'*62}")

        ok, segundos = ejecutar_paso(paso, verbose=not args.silencioso)

        t_str = f"{int(segundos//60)}m {int(segundos%60)}s"
        if ok:
            st = _estado_archivo(paso["output"])
            conteo_str = (f"{st['conteo']} items"
                          if st["existe"] else "⚠ output not found")
            msg_ok = f"  ✓ Step {paso['num']} completed"
            print(f"\n{VERDE(msg_ok)} in {t_str}  ({conteo_str})\n")
        else:
            msg_fallo = f"  ✗ Step {paso['num']} FAILED"
            print(f"\n{ROJO(msg_fallo)} in {t_str}")
            resp = input("  Continue to the next step? [y/N] ").strip().lower()
            if resp != "y":
                print("Pipeline stopped.")
                break

        resultados.append({"paso": paso["nombre"], "ok": ok, "tiempo": segundos})

    # ─── Final summary ────────────────────────────────────────────────────────
    t_total = time.time() - t_pipeline
    print(f"\n{'═'*62}")
    print(f"  PIPELINE SUMMARY")
    print(f"{'═'*62}")
    for r in resultados:
        ico   = VERDE("✓") if r["ok"] else ROJO("✗")
        t_str = f"{int(r['tiempo']//60)}m {int(r['tiempo']%60)}s"
        print(f"  {ico}  {r['paso']:<14}  {t_str}")

    print(f"{'─'*62}")
    print(f"  Total time: {int(t_total//60)}m {int(t_total%60)}s")

    todos_ok = all(r["ok"] for r in resultados)
    if todos_ok:
        print(f"\n  {VERDE('Pipeline complete. Generated files:')}")
        mostrar_estado()
        print(f"  Next steps:")
        print(f"    1. python email_sender.py --limit 5    (test with 5 emails)")
        print(f"    2. python email_sender.py --force       (full send)")
        print(f"    3. python calendar_manager.py --listar  (view calendar)")
    else:
        print(f"\n  {AMARILLO('Pipeline completed with errors. Check failed steps.')}")

    print()


if __name__ == "__main__":
    main()
