"""
Lead scoring for solar panel installation.
Uses claude-haiku-4-5 (Anthropic API) to score each company from 1 to 10.
Companies with score >= THRESHOLD are saved to qualified_leads.json.
Supports resume: if scoring_checkpoint.json exists, picks up where it left off.
"""

import json
import sys
import os
import time
import re
import logging
from pathlib import Path
from dotenv import load_dotenv
import anthropic

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ─── Configuration ───────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    raise SystemExit("ERROR: ANTHROPIC_API_KEY not found in .env")

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"

sys.path.insert(0, str(ROOT_DIR))
from utils import barra

MODELO      = "claude-haiku-4-5"
UMBRAL      = 7
INPUT_FILE  = DATA_DIR / "companies.json"
OUTPUT_FILE = DATA_DIR / "qualified_leads.json"
CHECKPOINT  = DATA_DIR / "scoring_checkpoint.json"
TIMEOUT_SEG = 60
REINTENTOS  = 2

# Companies unable to decide locally (franchises, national chains)
CADENAS_EXCLUIDAS = {
    "ikea", "toyota", "euromaster", "midas", "first stop", "midas caceres",
    "mrw", "seur", "correos express", "dhl", "ups", "fedex",
    "el corte ingles", "carrefour", "mercadona", "lidl", "aldi",
    "repsol", "bp", "shell", "cepsa",
    "codere", "loterias", "once",
    "bosch car service",
    "salvador escoda",
    "dekra", "applus",
}

# Words indicating non-geriatric care homes (students, university)
RESIDENCIAS_EXCLUIDAS = {
    "universitari", "estudiante", "colegiomayor", "colegio mayor",
    "residencia de estudiantes", "campus",
}

# Sector boost: adjusts minimum score after Claude scoring
# (applied AFTER Claude score, not replacing it)
BOOST_SECTOR: dict[str, int] = {
    "cooperativa_agricola": 1,   # +1 point: proven high consumption
    "nave_industrial":      1,   # +1 point: large roofs guaranteed
    "residencia_mayores":   0,
    "taller_mecanico":     -1,   # -1 point: many are small premises
    "hotel_rural":          0,
}

PROMPT_SISTEMA = """Puntúa empresas españolas para instalación de placas solares.
JSON estricto: {"puntuacion": <1-10>, "razon": "<15 palabras>"}
9-10=cooperativas/fábricas/residencias geriátricas; 7-8=hoteles/almacenes/talleres medianos; 5-6=oficinas/locales; 1-4=franquicias/bajo consumo"""

PROMPT_USUARIO = """Empresa: {nombre}
Sector: {sector}
Tipos: {tipos}"""


# ─── Exclusion filters ────────────────────────────────────────────────────────

def es_cadena_nacional(nombre: str) -> bool:
    nombre_lower = nombre.lower()
    return any(cadena in nombre_lower for cadena in CADENAS_EXCLUIDAS)


def es_residencia_no_geriatrica(nombre: str, sector: str) -> bool:
    if "residencia" not in sector and "mayores" not in sector:
        return False
    nombre_lower = nombre.lower().replace(" ", "")
    return any(exc in nombre.lower() or exc.replace(" ", "") in nombre_lower
               for exc in RESIDENCIAS_EXCLUIDAS)


def debe_excluirse(empresa: dict) -> tuple[bool, str]:
    nombre = empresa.get("nombre", "")
    sector = empresa.get("sector", "")
    if es_cadena_nacional(nombre):
        return True, "national chain (centralised decision)"
    if es_residencia_no_geriatrica(nombre, sector):
        return True, "student residence (not geriatric)"
    return False, ""


# ─── Claude API ──────────────────────────────────────────────────────────────

_cliente_anthropic: anthropic.Anthropic | None = None

def _cliente() -> anthropic.Anthropic:
    global _cliente_anthropic
    if _cliente_anthropic is None:
        _cliente_anthropic = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _cliente_anthropic


def llamar_claude(empresa: dict) -> tuple[int, str]:
    tipos_str = ", ".join(empresa.get("tipos", [])[:5])
    prompt = PROMPT_USUARIO.format(
        nombre  = empresa.get("nombre", ""),
        sector  = empresa.get("sector", ""),
        tipos   = tipos_str or "sin datos",
    )
    mensaje = _cliente().messages.create(
        model      = MODELO,
        max_tokens = 80,
        system     = PROMPT_SISTEMA,
        messages   = [{"role": "user", "content": prompt}],
    )
    contenido = mensaje.content[0].text.strip()

    match = re.search(r'\{[^}]+\}', contenido, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON: {contenido[:80]}")
    datos = json.loads(match.group())
    puntuacion = int(datos["puntuacion"])
    razon      = datos.get("razon", "")
    if not 1 <= puntuacion <= 10:
        raise ValueError(f"Score out of range: {puntuacion}")
    return puntuacion, razon


def puntuar_con_reintentos(empresa: dict) -> tuple[int, str]:
    for intento in range(1, REINTENTOS + 2):
        try:
            return llamar_claude(empresa)
        except Exception as exc:
            if intento <= REINTENTOS:
                time.sleep(2)
            else:
                return 5, f"error: {str(exc)[:40]}"


def aplicar_boost(puntuacion: int, sector: str) -> int:
    boost = BOOST_SECTOR.get(sector, 0)
    return max(1, min(10, puntuacion + boost))


# ─── Checkpoint ──────────────────────────────────────────────────────────────

def cargar_checkpoint() -> tuple[set[str], list[dict]]:
    """Returns (already-processed place_ids, accumulated leads)."""
    if not CHECKPOINT.exists():
        return set(), []
    data = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    procesados = set(data.get("procesados", []))
    leads      = data.get("leads", [])
    return procesados, leads


def guardar_checkpoint(procesados: set[str], leads: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(
        json.dumps({"procesados": list(procesados), "leads": leads},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    empresas: list[dict] = json.loads(INPUT_FILE.read_text(encoding="utf-8"))
    total = len(empresas)

    procesados, leads = cargar_checkpoint()
    if procesados:
        print(f"Resuming checkpoint: {len(procesados)} already processed, {len(leads)} leads accumulated")

    print(f"Loaded {total} companies  |  Model: {MODELO} (Claude API)  |  Threshold: >= {UMBRAL}/10\n")

    excluidas = 0
    descartadas = 0
    errores = 0
    t_inicio = time.time()
    i_efectivo = len(procesados)

    for i, empresa in enumerate(empresas, 1):
        place_id = empresa.get("place_id", f"_idx_{i}")

        # Skip already processed (resume)
        if place_id in procesados:
            continue

        i_efectivo += 1

        # Pre-Claude exclusion filter
        excluir, motivo = debe_excluirse(empresa)
        if excluir:
            excluidas += 1
            procesados.add(place_id)
            estado_str = f"EXCL({motivo[:20]})"
            print(
                f"\r{barra(i, total)}  {estado_str:<26} {empresa['nombre'][:24]:<24}  ",
                end="", flush=True,
            )
            continue

        puntuacion, razon = puntuar_con_reintentos(empresa)

        # Apply sector boost
        sector   = empresa.get("sector", "")
        puntuacion_final = aplicar_boost(puntuacion, sector)

        empresa_copia = {**empresa, "puntuacion": puntuacion_final, "razon_scoring": razon,
                         "modelo_scoring": MODELO}

        es_lead = puntuacion_final >= UMBRAL
        if es_lead:
            leads.append(empresa_copia)
        else:
            descartadas += 1
        if "error:" in razon:
            errores += 1

        procesados.add(place_id)

        transcurrido = time.time() - t_inicio
        pendientes_reales = total - len(procesados)
        vel = i_efectivo / transcurrido if transcurrido > 0 else 1
        eta_seg = int(pendientes_reales / vel)
        eta_str = f"{eta_seg//60}m{eta_seg%60:02d}s"

        estado = "LEAD" if es_lead else "    "
        print(
            f"\r{barra(i, total)}  {estado} {puntuacion_final}/10  "
            f"{empresa['nombre'][:26]:<26}  ETA {eta_str}   ",
            end="", flush=True,
        )

        if i_efectivo % 10 == 0:
            guardar_checkpoint(procesados, leads)

    print()

    # Clean up checkpoint on successful completion
    if CHECKPOINT.exists():
        CHECKPOINT.unlink()

    leads_ordenados = sorted(leads, key=lambda e: e["puntuacion"], reverse=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(leads_ordenados, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    transcurrido_total = time.time() - t_inicio
    print(f"\n{'='*62}")
    print(f"  Companies processed  : {total}")
    print(f"  Excluded (filter)    : {excluidas}")
    print(f"  Qualified leads      : {len(leads)} ({len(leads)*100//max(total-excluidas,1)}%)")
    print(f"  Discarded            : {descartadas}")
    print(f"  Claude errors        : {errores}")
    print(f"  Total time           : {int(transcurrido_total//60)}m {int(transcurrido_total%60)}s")
    print(f"  Saved to             : {OUTPUT_FILE.name}")
    print(f"{'='*62}")

    print("\nTop 10 leads:")
    for e in leads_ordenados[:10]:
        print(f"  {e['puntuacion']}/10  {e['nombre']:<38}  {e['razon_scoring']}")


if __name__ == "__main__":
    main()
