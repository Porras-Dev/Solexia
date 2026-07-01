"""
Real solar production calculation using the free PVGIS API (JRC/EU).

For each lead in qualified_leads.json:
  - Calls PVGIS PVcalc with the business's real coordinates
  - Retrieves monthly and annual production (kWh)
  - Calculates savings in euros, payback years and CO2 avoided
  - Determines recommended installation size (kWp)

No API key required. Respectful of the public service (delay between requests).
Supports resume: does not reprocess leads that already have PVGIS data.

Output: leads_with_solar_data.json

Usage:
    python solar_calculator.py               # processes all leads
    python solar_calculator.py --limit 10    # first 10 only
    python solar_calculator.py --force       # reprocesses even if data exists
"""

import json
import sys
import os
import time
import math
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = ROOT_DIR / "logs"

sys.path.insert(0, str(ROOT_DIR))
from utils import barra

# ─── Logging ─────────────────────────────────────────────────────────────────

LOG_FILE = LOGS_DIR / "pvgis.log"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────────────

PVGIS_URL      = "https://re.jrc.ec.europa.eu/api/v5_2/PVcalc"
INPUT_FILE     = DATA_DIR / "qualified_leads.json"
OUTPUT_FILE    = DATA_DIR / "leads_with_solar_data.json"

PRECIO_KWH     = float(os.getenv("PRECIO_KWH", "0.18"))   # €/kWh (Spain average 2024)
TASA_AUTOCONSUMO = 0.85                                     # 85% production self-consumed
COSTE_POR_KWP  = 750                                        # €/kWp commercial installation
CO2_POR_KWH    = 0.233                                      # kg CO2/kWh (Red Eléctrica España 2023)
PERDIDAS_SISTEMA = 14                                       # % losses (shading, temperature, cabling)
INCLINACION    = 30                                         # degrees (optimal for latitude 39°N)
ORIENTACION    = 0                                          # 0 = south, 90 = west, -90 = east

DELAY_ENTRE_PETICIONES = 1.5   # seconds (respectful of public API)
TIMEOUT_PVGIS  = 30
MAX_REINTENTOS = 3

# Recommended installation size (kWp) by sector
# Based on typical consumption for each business type in Spain
KWP_POR_SECTOR: dict[str, int] = {
    "cooperativa_agricola":        75,   # irrigation motors + refrigeration + processing
    "cooperativa agricola caceres": 75,
    "nave_industrial":             50,   # machinery + lighting + HVAC
    "nave industrial caceres":     50,
    "residencia_mayores":          40,   # 24h HVAC + industrial kitchen + laundry
    "residencia de mayores caceres": 40,
    "taller_mecanico":             15,   # compressors + lifts + diagnostics
    "taller mecanico caceres":     15,
    "hotel_rural":                 25,   # HVAC + pool + lighting
    "hotel rural extremadura":     25,
}
KWP_DEFAULT = 30   # fallback if sector not mapped

MESES_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

# ─── PVGIS ───────────────────────────────────────────────────────────────────

def kwp_para_sector(empresa: dict) -> int:
    sector = empresa.get("sector", "").lower().strip()
    return KWP_POR_SECTOR.get(sector, KWP_DEFAULT)


def llamar_pvgis(lat: float, lng: float, kwp: float) -> dict:
    """Calls PVGIS and returns the raw JSON. Raises exception on failure."""
    params = {
        "lat":            round(lat, 6),
        "lon":            round(lng, 6),
        "peakpower":      kwp,
        "loss":           PERDIDAS_SISTEMA,
        "pvtechchoice":   "crystSi",
        "mountingplace":  "building",
        "angle":          INCLINACION,
        "aspect":         ORIENTACION,
        "outputformat":   "json",
        "browser":        0,
    }
    resp = requests.get(PVGIS_URL, params=params, timeout=TIMEOUT_PVGIS)
    if resp.status_code == 429:
        raise RuntimeError("PVGIS rate limit (429) — wait a few minutes")
    resp.raise_for_status()
    return resp.json()


def _coordenadas_validas(lat: float, lng: float) -> bool:
    """Checks that coordinates fall within the Iberian Peninsula + Canary Islands."""
    return -10.0 <= lng <= 5.0 and 27.5 <= lat <= 44.0


def pvgis_con_reintentos(lat: float, lng: float, kwp: float) -> dict | None:
    for intento in range(1, MAX_REINTENTOS + 1):
        try:
            return llamar_pvgis(lat, lng, kwp)
        except RuntimeError as exc:
            log.warning(f"  {exc}")
            time.sleep(60)   # rate limit: wait 1 min
        except requests.HTTPError as exc:
            log.warning(f"  HTTP {exc.response.status_code} on attempt {intento}")
            if exc.response.status_code in (400, 404):
                return None   # permanent error, do not retry
            time.sleep(3 * intento)
        except requests.Timeout:
            log.warning(f"  Timeout on attempt {intento}")
            time.sleep(3 * intento)
        except Exception as exc:
            log.error(f"  Unexpected error: {exc}")
            return None
    return None


# ─── Economic calculation ────────────────────────────────────────────────────

def calcular_economia(kwh_anuales: float, kwp: float) -> dict:
    ahorro_anual    = kwh_anuales * TASA_AUTOCONSUMO * PRECIO_KWH
    coste_total     = kwp * COSTE_POR_KWP
    anos_amort      = coste_total / ahorro_anual if ahorro_anual > 0 else 99
    ahorro_20_anos  = ahorro_anual * 20 - coste_total   # net benefit over 20 years
    co2_evitado_kg  = kwh_anuales * CO2_POR_KWH
    num_paneles     = math.ceil(kwp * 1000 / 400)       # 400 Wp panels
    area_m2         = kwp * 7                            # ~7 m²/kWp on commercial roof

    return {
        "kwp_recomendado":      kwp,
        "num_paneles":          num_paneles,
        "area_m2_estimada":     area_m2,
        "kwh_anuales":          round(kwh_anuales, 1),
        "ahorro_anual_eur":     round(ahorro_anual, 2),
        "coste_instalacion_eur": coste_total,
        "anos_amortizacion":    round(anos_amort, 1),
        "ahorro_neto_20anos_eur": round(ahorro_20_anos, 0),
        "co2_evitado_kg_ano":   round(co2_evitado_kg, 0),
        "precio_kwh_usado":     PRECIO_KWH,
        "tasa_autoconsumo":     TASA_AUTOCONSUMO,
    }


def procesar_pvgis(raw: dict, kwp: float) -> dict:
    """Extracts useful data from the PVGIS JSON and calculates economics."""
    totales  = raw["outputs"]["totals"]["fixed"]
    mensual  = raw["outputs"]["monthly"]["fixed"]

    kwh_anuales = totales["E_y"]
    produccion_mensual = [
        {
            "mes":    MESES_ES[m["month"] - 1],
            "kwh":    round(m["E_m"], 1),
        }
        for m in mensual
    ]

    economia = calcular_economia(kwh_anuales, kwp)
    return {
        **economia,
        "produccion_mensual": produccion_mensual,
        "h_irradiacion_anual": round(totales.get("H(i)_y", 0), 1),
        "fuente":              "PVGIS-SARAH3",
        "fecha_calculo":       datetime.now(timezone.utc).isoformat(),
    }


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true",
                        help="Reprocesses even if PVGIS data already exists")
    args = parser.parse_args()

    leads: list[dict] = json.loads(INPUT_FILE.read_text(encoding="utf-8"))

    # Load previous results if they exist (for resume)
    if OUTPUT_FILE.exists() and not args.force:
        previos = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        previos_map = {p["place_id"]: p for p in previos}
    else:
        previos_map = {}

    total = len(leads)
    if args.limit:
        leads = leads[:args.limit]
        total = len(leads)

    print(f"Loaded {total} leads | kWh price: {PRECIO_KWH} € | API: PVGIS\n")

    resultados:  list[dict] = []
    procesados   = 0
    reutilizados = 0
    errores      = 0
    sin_coords   = 0
    t_inicio     = time.time()

    for i, lead in enumerate(leads, 1):
        place_id = lead.get("place_id", "")
        nombre   = lead.get("nombre", "")[:30]

        # Resume: if PVGIS data already exists and not forced, reuse it
        if place_id in previos_map and not args.force:
            resultados.append(previos_map[place_id])
            reutilizados += 1
            print(f"\r{barra(i, total, ancho=28)}  [cache]  {nombre:<30}", end="", flush=True)
            continue

        lat = lead.get("lat")
        lng = lead.get("lng")

        if not lat or not lng:
            sin_coords += 1
            resultados.append({**lead, "pvgis": None, "pvgis_error": "no coordinates"})
            print(f"\r{barra(i, total, ancho=28)}  [!coord] {nombre:<30}", end="", flush=True)
            continue

        # Validate coordinates correspond to Iberian Peninsula / Canary Islands
        try:
            lat_f, lng_f = float(lat), float(lng)
        except (TypeError, ValueError):
            sin_coords += 1
            resultados.append({**lead, "pvgis": None, "pvgis_error": "invalid coordinates"})
            print(f"\r{barra(i, total, ancho=28)}  [!coord] {nombre:<30}", end="", flush=True)
            continue

        if not _coordenadas_validas(lat_f, lng_f):
            sin_coords += 1
            resultados.append({**lead, "pvgis": None,
                                "pvgis_error": f"coordinates out of range: {lat_f},{lng_f}"})
            log.warning(f"  Coordinates outside Spain: {nombre} ({lat_f},{lng_f})")
            print(f"\r{barra(i, total, ancho=28)}  [!range] {nombre:<30}", end="", flush=True)
            continue

        kwp = kwp_para_sector(lead)
        raw = pvgis_con_reintentos(lat, lng, kwp)

        if raw is None:
            errores += 1
            resultados.append({**lead, "pvgis": None, "pvgis_error": "API error"})
            log.warning(f"  No PVGIS data: {nombre}")
            print(f"\r{barra(i, total, ancho=28)}  [error]  {nombre:<30}", end="", flush=True)
        else:
            datos_pvgis = procesar_pvgis(raw, kwp)
            resultados.append({**lead, "pvgis": datos_pvgis, "pvgis_error": None})
            procesados += 1

            kwh  = datos_pvgis["kwh_anuales"]
            eur  = datos_pvgis["ahorro_anual_eur"]
            anos = datos_pvgis["anos_amortizacion"]
            transcurrido = time.time() - t_inicio
            pendientes   = total - i
            vel          = (i - reutilizados) / transcurrido if transcurrido > 0 else 1
            eta          = int(pendientes * (DELAY_ENTRE_PETICIONES + 1 / max(vel, 0.1)))
            print(
                f"\r{barra(i, total, ancho=28)}  {kwh:>8.0f} kWh  {eur:>7.0f} €/año  "
                f"{anos:.1f}a  ETA {eta//60}m{eta%60:02d}s  {nombre:<26}",
                end="", flush=True,
            )

        # Save after each request (safe resume, atomic write)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _tmp = OUTPUT_FILE.with_suffix(".tmp")
        _tmp.write_text(json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")
        _tmp.replace(OUTPUT_FILE)

        # Respectful delay for PVGIS (real requests only)
        if raw is not None and i < total:
            time.sleep(DELAY_ENTRE_PETICIONES)

    print()

    # ─── Summary ─────────────────────────────────────────────────────────────
    total_tiempo = time.time() - t_inicio
    con_pvgis    = [r for r in resultados if r.get("pvgis")]
    kwh_total    = sum(r["pvgis"]["kwh_anuales"]    for r in con_pvgis)
    eur_total    = sum(r["pvgis"]["ahorro_anual_eur"] for r in con_pvgis)
    co2_total    = sum(r["pvgis"]["co2_evitado_kg_ano"] for r in con_pvgis) / 1000

    print(f"\n{'='*68}")
    print(f"  Leads processed with PVGIS : {procesados}  (cache: {reutilizados})")
    print(f"  Without coordinates        : {sin_coords}")
    print(f"  API errors                 : {errores}")
    print(f"  Total time                 : {int(total_tiempo//60)}m {int(total_tiempo%60)}s")
    print(f"{'─'*68}")
    print(f"  Total estimated production : {kwh_total:>12,.0f} kWh/year")
    print(f"  Aggregated savings         : {eur_total:>12,.0f} €/year")
    print(f"  CO₂ avoided (portfolio)    : {co2_total:>12.1f} t CO₂/year")
    print(f"  Saved to                   : {OUTPUT_FILE.name}")
    print(f"{'='*68}")

    # Top 10 by savings
    top = sorted(con_pvgis, key=lambda r: r["pvgis"]["ahorro_anual_eur"], reverse=True)[:10]
    print("\nTop 10 by annual savings:")
    for r in top:
        p = r["pvgis"]
        print(f"  {p['ahorro_anual_eur']:>8,.0f} €/year  {p['kwh_anuales']:>8,.0f} kWh  "
              f"{p['anos_amortizacion']:>4.1f}y  {r['nombre'][:38]}")


if __name__ == "__main__":
    main()
