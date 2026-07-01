"""
Personalised cold email generator for solar leads.

Uses Claude API (claude-haiku-4-5) to draft each email uniquely,
with real context: name, sector, municipality and estimated savings by consumption.
Sender details read from .env.

Output: generated_emails.json
"""

import json
import re
import sys
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
import anthropic

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    raise SystemExit("ERROR: ANTHROPIC_API_KEY not found in .env")

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"

sys.path.insert(0, str(ROOT_DIR))
from utils import barra

MODELO            = "claude-haiku-4-5"

EMPRESA_NOMBRE   = os.getenv("EMPRESA_NOMBRE",   "SolarCáceres")
EMPRESA_TELEFONO = os.getenv("EMPRESA_TELEFONO", "927 000 000")
EMPRESA_WEB      = os.getenv("EMPRESA_WEB",      "www.solarcaceres.es")
EMPRESA_EMAIL    = os.getenv("EMPRESA_EMAIL",     "info@solarcaceres.es")

INPUT_FILE  = DATA_DIR / "qualified_leads.json"
OUTPUT_FILE = DATA_DIR / "generated_emails.json"

REINTENTOS = 3

PROMPT_SISTEMA = """Redacta emails fríos de venta de placas solares en España. Tono profesional-cercano, 4 párrafos cortos.
Responde SOLO en JSON: {"asunto": "<asunto>", "cuerpo": "<cuerpo completo con saludo final>"}"""

PROMPT_SISTEMA_ESTRICTO = (
    "Responde ÚNICAMENTE con un objeto JSON válido. Sin texto antes ni después.\n"
    'Formato exacto (sin variaciones): {"asunto": "texto", "cuerpo": "texto"}\n'
    "El cuerpo es un email de venta de placas solares en España, tono formal (usted), 4 párrafos cortos."
)

# ─── Domains to exclude as contact ────────────────────────────────────────────

DOMINIOS_EXCLUIDOS = {
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "linkedin.com", "youtube.com", "tiktok.com",
    "webnode.es", "wix.com", "weebly.com", "jimdo.com",
    "google.com", "yelp.com", "tripadvisor.com",
    "toyota.es", "toyota.com", "euromaster-neumaticos.es",
    "ikea.com", "uberall.com",
}

# ─── Savings estimates by sector ───────────────────────────────────────────────

AHORROS_SECTOR = {
    "cooperativa_agricola": {
        "rango":  (9_000, 22_000),
        "motivo": "motores de riego, cámaras frigoríficas y maquinaria agrícola",
        "label":  "cooperativa agrícola",
        "consumo": "alto consumo eléctrico continuo en producción y almacenamiento",
    },
    "nave_industrial": {
        "rango":  (6_000, 15_000),
        "motivo": "maquinaria, iluminación industrial y climatización",
        "label":  "empresa industrial",
        "consumo": "consumo elevado en producción y sistemas de ventilación",
    },
    "taller_mecanico": {
        "rango":  (2_800, 6_500),
        "motivo": "compresores, elevadores, equipos de diagnóstico e iluminación",
        "label":  "taller mecánico",
        "consumo": "consumo intensivo en compresores y equipamiento técnico",
    },
    "residencia_mayores": {
        "rango":  (8_000, 18_000),
        "motivo": "climatización 24 h, cocina industrial y lavandería",
        "label":  "residencia de mayores",
        "consumo": "demanda eléctrica constante las 24 horas del día",
    },
    "hotel_rural": {
        "rango":  (4_500, 10_000),
        "motivo": "climatización, piscina e iluminación exterior",
        "label":  "hotel rural",
        "consumo": "consumo en climatización, hostelería y zonas comunes",
    },
    # Aliases from the previous scraper
    "cooperativa agrícola":  "cooperativa_agricola",
    "empresa industrial":    "nave_industrial",
    "taller mecánico":       "taller_mecanico",
    "residencia de mayores": "residencia_mayores",
    "hotel rural":           "hotel_rural",
}


# ─── Utilities ──────────────────────────────────────────────────────────────

def _resolver_clave(empresa: dict) -> str:
    sector = empresa.get("sector", "")
    if sector in AHORROS_SECTOR and isinstance(AHORROS_SECTOR[sector], dict):
        return sector
    if sector in AHORROS_SECTOR:
        return AHORROS_SECTOR[sector]
    texto = " ".join([sector, empresa.get("nombre", ""), *empresa.get("tipos", [])]).lower()
    if any(k in texto for k in ("cooperativ", "coop", "almazara", "agricol")):
        return "cooperativa_agricola"
    if any(k in texto for k in ("residencia", "geriátri", "mayores", "ancianos", "hogar")):
        return "residencia_mayores"
    if any(k in texto for k in ("hotel", "rural", "alojamiento", "hostal")):
        return "hotel_rural"
    if any(k in texto for k in ("taller", "mecanico", "mecánico", "automocion",
                                 "neumátic", "garage", "motor", "automoción")):
        return "taller_mecanico"
    return "nave_industrial"


def _ahorro_para(empresa: dict, clave: str) -> tuple[str, str]:
    datos  = AHORROS_SECTOR[clave]
    lo, hi = datos["rango"]
    score  = empresa.get("puntuacion", 8)
    factor = min(0.70 + (score - 7) * 0.15, 1.0)
    amin   = int(lo + (hi - lo) * 0.3)
    amax   = int(lo + (hi - lo) * factor)
    return f"{amin:,.0f}".replace(",", "."), f"{amax:,.0f}".replace(",", ".")


def _email_valido(web: str | None) -> str | None:
    if not web:
        return None
    try:
        dominio = urlparse(web).netloc.lstrip("www.")
        if not dominio:
            return None
        dominio_base = ".".join(dominio.split(".")[-2:])
        if dominio_base in DOMINIOS_EXCLUIDOS or dominio in DOMINIOS_EXCLUIDOS:
            return None
        return f"info@{dominio}"
    except Exception:
        return None


def _municipio(direccion: str) -> str:
    for parte in reversed([p.strip() for p in direccion.split(",")]):
        parte = parte.strip()
        if not any(c.isdigit() for c in parte) and len(parte) > 3:
            if parte.lower() in ("españa", "spain", "extremadura"):
                continue
            return parte.title()
    return "Cáceres"


def _nombre_corto(nombre: str) -> str:
    stop = {
        "sociedad", "cooperativa", "s.l.", "sl", "s.a.", "sa", "cb", "c.b.",
        "s.coop.", "scl", "s.c.l.", "ltda", "s.coop", "coop.",
        "de", "la", "el", "los", "las", "nuestro", "nuestra",
    }
    palabras = [w for w in nombre.split() if w.lower() not in stop]
    return " ".join(palabras[:3]) if palabras else nombre[:25]


# ─── Email parsing and fallback ──────────────────────────────────────────────

def _extraer_texto_plano(contenido: str) -> tuple[str, str] | None:
    """Tries to extract subject and body from a non-JSON Claude response."""
    # Case 1: explicit "Asunto:" label (with or without markdown asterisks)
    m = re.search(r'(?im)^\s*\*{0,2}asunto\*{0,2}\s*:[ \t]*(.+?)[ \t]*$', contenido)
    if m:
        asunto = m.group(1).strip().strip('*"')
        resto  = contenido[m.end():].lstrip('\n')
        # Remove "Cuerpo:" label if present
        resto  = re.sub(r'(?im)^\s*\*{0,2}cuerpo\*{0,2}\s*:[ \t]*\n?', '', resto, count=1)
        cuerpo = resto.strip()
        if asunto and len(cuerpo) > 80:
            return asunto, cuerpo

    # Case 2: no labels — first short line as subject, rest as body
    lineas = contenido.strip().split('\n')
    no_vacias = [l.strip() for l in lineas if l.strip()]
    if len(no_vacias) >= 4:
        primera = no_vacias[0].lstrip('#*- "').strip().rstrip('"')
        if 15 <= len(primera) <= 140:
            idx    = next(i for i, l in enumerate(lineas) if l.strip() == no_vacias[0])
            cuerpo = '\n'.join(lineas[idx + 1:]).strip()
            if len(cuerpo) > 80:
                return primera, cuerpo

    return None


def _email_fallback(nombre: str, municipio: str, datos: dict,
                    amin_s: str, amax_s: str) -> tuple[str, str]:
    """Reasonable-quality email generated without Claude when all retries fail."""
    ncorto = _nombre_corto(nombre)
    asunto = f"Reducción de costes energéticos para {ncorto} — hasta {amax_s} €/año"
    cuerpo = (
        f"Estimado equipo de {nombre},\n\n"
        f"Nos ponemos en contacto con usted porque {datos['label']}s como {ncorto}, en {municipio}, "
        f"están logrando ahorros energéticos muy significativos gracias a la energía solar fotovoltaica.\n\n"
        f"Teniendo en cuenta el consumo habitual de su sector —{datos['motivo']}—, "
        f"estimamos que su empresa podría ahorrar entre {amin_s} y {amax_s} euros al año "
        f"en su factura eléctrica. Un ahorro directo que se mantiene durante toda la vida útil de la instalación.\n\n"
        f"La instalación no requiere inversión inicial: trabajamos con financiación mediante leasing a 0 €, "
        f"con amortización completa en 5 a 7 años. A partir de entonces, la energía generada "
        f"es prácticamente gratuita.\n\n"
        f"Si desea recibir un estudio gratuito y sin compromiso adaptado a su situación, "
        f"llámenos al {EMPRESA_TELEFONO} o visítenos en {EMPRESA_WEB}. "
        f"Le preparamos el análisis en 48 horas.\n\n"
        f"Atentamente,\n"
        f"{EMPRESA_NOMBRE}\n"
        f"{EMPRESA_WEB}"
    )
    return asunto, cuerpo


# ─── Claude API ──────────────────────────────────────────────────────────────

_cliente_anthropic: anthropic.Anthropic | None = None


def _cliente() -> anthropic.Anthropic:
    global _cliente_anthropic
    if _cliente_anthropic is None:
        _cliente_anthropic = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _cliente_anthropic


def generar_email_claude(empresa: dict) -> tuple[str, str, str]:
    clave          = _resolver_clave(empresa)
    datos          = AHORROS_SECTOR[clave]
    amin_s, amax_s = _ahorro_para(empresa, clave)
    nombre         = empresa.get("nombre", "")
    municipio      = _municipio(empresa.get("direccion", ""))

    prompt_base = (
        f"Empresa: {nombre} ({datos['label']}), {municipio}\n"
        f"Ahorro estimado: {amin_s}–{amax_s} €/año en {datos['motivo']}\n"
        f"Remitente: {EMPRESA_NOMBRE}, tel. {EMPRESA_TELEFONO}, {EMPRESA_WEB}\n\n"
        "Tratamiento: siempre formal, usted/su empresa, nunca tú ni vosotros.\n"
        "Párrafos: 1) apertura específica al sector y localidad "
        "2) ahorro concreto con los números y consumos principales "
        "3) sin inversión inicial, leasing a 0€, amortización 5-7 años "
        "4) CTA con teléfono y oferta de estudio gratuito en 48h"
    )

    for intento in range(REINTENTOS + 1):
        # Last attempt: stricter prompt and system to force JSON
        es_ultimo = (intento == REINTENTOS)
        sistema = PROMPT_SISTEMA_ESTRICTO if es_ultimo else PROMPT_SISTEMA
        prompt  = (
            f"Responde SOLO con JSON válido, sin ningún texto adicional.\n\n{prompt_base}"
            if es_ultimo else prompt_base
        )

        try:
            msg = _cliente().messages.create(
                model      = MODELO,
                max_tokens = 400,
                system     = sistema,
                messages   = [{"role": "user", "content": prompt}],
            )
            contenido = msg.content[0].text.strip()

            # Priority 1: standard JSON
            match = re.search(r'\{.*\}', contenido, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                return clave, parsed["asunto"], parsed["cuerpo"]

            # Priority 2: plain text with recognisable structure
            recuperado = _extraer_texto_plano(contenido)
            if recuperado:
                return clave, recuperado[0], recuperado[1]

            raise ValueError("No JSON or recognisable format in response")

        except Exception:
            if intento < REINTENTOS:
                time.sleep(2)

    # Final fallback: reasonable quality email without Claude
    asunto, cuerpo = _email_fallback(nombre, municipio, datos, amin_s, amax_s)
    return clave, asunto, cuerpo


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    leads: list[dict] = json.loads(INPUT_FILE.read_text(encoding="utf-8"))
    total = len(leads)
    print(f"Loaded {total} leads from {INPUT_FILE.name}")
    print(f"Model: {MODELO}  |  Sender: {EMPRESA_NOMBRE} | Tel: {EMPRESA_TELEFONO} | Web: {EMPRESA_WEB}\n")

    resultados: list[dict] = []
    t_inicio  = time.time()
    ahora_iso = datetime.now(timezone.utc).isoformat()
    errores   = 0

    for i, empresa in enumerate(leads):
        clave, asunto, cuerpo = generar_email_claude(empresa)
        email_contacto = _email_valido(empresa.get("web"))

        if "(Error generando email:" in cuerpo:
            errores += 1

        registro = {
            "id":               str(uuid.uuid4()),
            "place_id":         empresa.get("place_id", ""),
            "nombre":           empresa.get("nombre", ""),
            "email_contacto":   email_contacto,
            "telefono":         empresa.get("telefono"),
            "web":              empresa.get("web"),
            "sector":           AHORROS_SECTOR[clave]["label"],
            "puntuacion":       empresa.get("puntuacion"),
            "asunto":           asunto,
            "cuerpo":           cuerpo,
            "fecha_generacion": ahora_iso,
            "estado_envio":     "pendiente",
            "fecha_envio":      None,
            "respuesta":        None,
        }
        resultados.append(registro)

        tiene_email = "✉" if email_contacto else " "
        print(
            f"\r{barra(i+1, total)}  {tiene_email}  {empresa['nombre'][:34]:<34}",
            end="", flush=True,
        )

    print()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    transcurrido   = time.time() - t_inicio
    con_email      = sum(1 for r in resultados if r["email_contacto"])
    con_tel        = sum(1 for r in resultados if r["telefono"])
    asuntos_unicos = len(set(r["asunto"] for r in resultados))

    print(f"\n{'='*64}")
    print(f"  Emails generated       : {len(resultados)}")
    print(f"  With contact email     : {con_email}  ({con_email*100//total}%)")
    print(f"  With phone             : {con_tel}  ({con_tel*100//total}%)")
    print(f"  Unique subjects        : {asuntos_unicos} / {total}")
    print(f"  Claude errors          : {errores}")
    print(f"  Total time             : {transcurrido:.1f}s")
    print(f"  Saved to               : {OUTPUT_FILE.name}")
    print(f"{'='*64}")

    print("\n--- 3 example emails ---\n")
    for r in resultados[:3]:
        print(f"To:      {r['nombre']}")
        print(f"Email:   {r['email_contacto'] or '(no email in Google Maps)'}")
        print(f"Subject: {r['asunto']}")
        print(f"\n{r['cuerpo']}")
        print("-" * 60)


if __name__ == "__main__":
    main()
