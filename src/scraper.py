"""
Business scraper for the Cáceres area using Google Maps Places API (New).
Searches for solar panel installation candidates within an 80 km radius.
Requires: GOOGLE_MAPS_API_KEY in .env
"""

import os
import sys
import json
import math
import time
import logging
from pathlib import Path
from dotenv import load_dotenv
import requests

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────────────

API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
if not API_KEY:
    raise SystemExit("ERROR: GOOGLE_MAPS_API_KEY not found in .env")

CACERES_LAT   = 39.4753
CACERES_LNG   = -6.3724
RADIO_METROS  = 80_000

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"

# Pre-computed once
_DELTA_LAT = RADIO_METROS / 111_320
_DELTA_LNG = RADIO_METROS / (111_320 * math.cos(math.radians(CACERES_LAT)))

BBOX = {
    "low":  {"latitude": CACERES_LAT - _DELTA_LAT, "longitude": CACERES_LNG - _DELTA_LNG},
    "high": {"latitude": CACERES_LAT + _DELTA_LAT, "longitude": CACERES_LNG + _DELTA_LNG},
}

# Queries by sector with normalized label
# Format: (google_query, normalized_sector)
QUERIES_SECTORES = [
    ("cooperativa agricola Caceres",         "cooperativa_agricola"),
    ("almazara aceite Caceres Extremadura",   "cooperativa_agricola"),
    ("nave industrial Caceres",               "nave_industrial"),
    ("almacen industrial Extremadura",        "nave_industrial"),
    ("hotel rural Caceres",                   "hotel_rural"),
    ("casa rural Caceres Extremadura",        "hotel_rural"),
    ("turismo rural Extremadura",             "hotel_rural"),
    ("alojamiento rural Extremadura",         "hotel_rural"),
    ("taller mecanico Caceres",               "taller_mecanico"),
    ("taller automovil Caceres",              "taller_mecanico"),
    ("residencia mayores Caceres",            "residencia_mayores"),
    ("residencia ancianos Extremadura",       "residencia_mayores"),
]

OUTPUT_FILE = DATA_DIR / "companies.json"
SEARCH_URL  = "https://places.googleapis.com/v1/places:searchText"

FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.location",
    "places.businessStatus",
    "places.types",
    "nextPageToken",
])

# ─── Functions ───────────────────────────────────────────────────────────────

def buscar_texto(query: str, page_token: str | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body: dict = {
        "textQuery": query,
        "languageCode": "es",
        "pageSize": 20,
        "locationRestriction": {"rectangle": BBOX},
    }
    if page_token:
        body["pageToken"] = page_token

    for intento in range(3):
        try:
            resp = requests.post(SEARCH_URL, headers=headers, json=body, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except requests.Timeout:
            if intento < 2:
                time.sleep(3)
            else:
                raise
        except requests.HTTPError:
            raise


def normalizar(lugar: dict, sector: str) -> dict:
    loc = lugar.get("location", {})
    web = lugar.get("websiteUri")
    return {
        "place_id":  lugar.get("id", ""),
        "nombre":    lugar.get("displayName", {}).get("text", ""),
        "direccion": lugar.get("formattedAddress", ""),
        "lat":       loc.get("latitude"),
        "lng":       loc.get("longitude"),
        "sector":    sector,
        "tipos":     lugar.get("types", []),
        "estado":    lugar.get("businessStatus", "UNKNOWN"),
        "telefono":  lugar.get("nationalPhoneNumber") or lugar.get("internationalPhoneNumber"),
        "web":       web if web and not _es_red_social(web) else None,
        "email":     None,
    }


def _es_red_social(url: str) -> bool:
    dominios_excluidos = (
        "facebook.com", "instagram.com", "twitter.com", "linkedin.com",
        "youtube.com", "tiktok.com", "webnode.es", "wix.com", "weebly.com",
    )
    return any(d in url.lower() for d in dominios_excluidos)


def scrape_sector(query: str, sector: str) -> list[dict]:
    resultados = []
    page_token = None
    pagina = 1

    while True:
        log.info(f"  [{sector}] '{query}' — page {pagina}...")
        try:
            data = buscar_texto(query, page_token)
        except requests.HTTPError as exc:
            log.warning(f"  HTTP error: {exc}")
            break
        except requests.Timeout:
            log.warning(f"  Timeout on page {pagina}, aborting sector")
            break

        lugares = data.get("places", [])
        if not lugares:
            log.info("  No results")
            break

        for lugar in lugares:
            resultados.append(normalizar(lugar, sector))

        page_token = data.get("nextPageToken")
        if not page_token:
            break

        pagina += 1
        time.sleep(2)

    return resultados


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    log.info(f"Starting scrape | radius {RADIO_METROS//1000} km from Cáceres")
    log.info(f"Queries: {len(QUERIES_SECTORES)}")

    todas:  list[dict] = []
    vistas: set[str]  = set()

    for query, sector in QUERIES_SECTORES:
        log.info(f"Sector: {sector}")
        empresas = scrape_sector(query, sector)
        nuevas = [e for e in empresas if e["place_id"] not in vistas]
        vistas.update(e["place_id"] for e in nuevas)
        todas.extend(nuevas)
        log.info(f"  +{len(nuevas)} new (running total: {len(todas)})")

    activas = [e for e in todas if e["estado"] != "CLOSED_PERMANENTLY"]
    log.info(f"Active businesses: {len(activas)} of {len(todas)}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(activas, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(f"Saved to {OUTPUT_FILE}")

    print("\n--- Summary by sector ---")
    sectores_unicos = sorted(set(e["sector"] for e in activas))
    for sector in sectores_unicos:
        n = sum(1 for e in activas if e["sector"] == sector)
        print(f"  {sector:<30} {n:>3} businesses")
    print(f"\n  TOTAL  {len(activas)} businesses  ->  {OUTPUT_FILE.name}")


if __name__ == "__main__":
    main()
