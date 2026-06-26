"""
Professional PDF proposal generator using ReportLab.

Reads leads_with_solar_data.json and generates one PDF per lead in data/proposals/.
Each PDF includes: branded cover, real savings KPIs (PVGIS data),
monthly production chart, detailed economic table and CTA.

Requires: pip install reportlab

Usage:
    python pdf_generator.py                   # generates all PDFs
    python pdf_generator.py --limit 5         # first 5 only
    python pdf_generator.py --solo-con-pvgis  # skip leads without PVGIS data
    python pdf_generator.py --output ./pdfs   # custom output directory
"""

import json
import sys
import os
import re
import math
import time
import argparse
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"

sys.path.insert(0, str(ROOT_DIR))
from utils import barra

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm, mm
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.graphics.shapes import Drawing, Rect, String, Line, Group
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.textlabels import Label
    from reportlab.graphics import renderPDF
except ImportError:
    print("ERROR: ReportLab is not installed.")
    print("  pip install reportlab")
    sys.exit(1)

# ─── Configuration ───────────────────────────────────────────────────────────

INPUT_FILE   = DATA_DIR / "leads_with_solar_data.json"

EMPRESA_NOMBRE   = os.getenv("EMPRESA_NOMBRE",   "SolarCáceres")
EMPRESA_TELEFONO = os.getenv("EMPRESA_TELEFONO", "927 000 000")
EMPRESA_WEB      = os.getenv("EMPRESA_WEB",      "www.solarcaceres.es")
EMPRESA_EMAIL    = os.getenv("EMPRESA_EMAIL",     "info@solarcaceres.es")

# Color palette
NARANJA     = colors.HexColor("#E87D1A")
NARANJA_OSC = colors.HexColor("#C46510")
AZUL_OSC    = colors.HexColor("#1E3A5F")
AZUL_MED    = colors.HexColor("#2E5FA3")
GRIS_OSC    = colors.HexColor("#3D3D3D")
GRIS_MED    = colors.HexColor("#6B6B6B")
GRIS_CLARO  = colors.HexColor("#F2F2F2")
VERDE       = colors.HexColor("#27AE60")
BLANCO      = colors.white

# A4 dimensions
PAG_W, PAG_H = A4               # 595.27 x 841.89 pt
MARGEN       = 1.8 * cm
ANCHO        = PAG_W - 2 * MARGEN


# ─── Utilities ──────────────────────────────────────────────────────────────

def sanitizar_nombre(nombre: str) -> str:
    nombre = nombre.lower()
    nombre = re.sub(r'[^\w\s-]', '', nombre, flags=re.UNICODE)
    nombre = re.sub(r'\s+', '_', nombre.strip())
    return nombre[:50]


def fmt_eur(valor: float) -> str:
    """Formats a number as €: 22.000 €"""
    return f"{valor:,.0f}".replace(",", ".") + " €"


def fmt_kwh(valor: float) -> str:
    return f"{valor:,.0f}".replace(",", ".") + " kWh"


def truncar(texto: str, max_chars: int) -> str:
    return texto if len(texto) <= max_chars else texto[:max_chars - 1] + "…"


# ─── Fallback data (when no PVGIS) ─────────────────────────────────────────

AHORROS_ESTIMADOS = {
    "cooperativa_agricola":          {"kwh": 97_500, "eur": 14_873, "kwp": 75, "anos": 3.8},
    "cooperativa agricola caceres":  {"kwh": 97_500, "eur": 14_873, "kwp": 75, "anos": 3.8},
    "nave_industrial":               {"kwh": 65_000, "eur":  9_916, "kwp": 50, "anos": 3.8},
    "nave industrial caceres":       {"kwh": 65_000, "eur":  9_916, "kwp": 50, "anos": 3.8},
    "residencia_mayores":            {"kwh": 52_000, "eur":  7_933, "kwp": 40, "anos": 3.8},
    "residencia de mayores caceres": {"kwh": 52_000, "eur":  7_933, "kwp": 40, "anos": 3.8},
    "taller_mecanico":               {"kwh": 19_500, "eur":  2_975, "kwp": 15, "anos": 3.8},
    "taller mecanico caceres":       {"kwh": 19_500, "eur":  2_975, "kwp": 15, "anos": 3.8},
    "hotel_rural":                   {"kwh": 32_500, "eur":  4_958, "kwp": 25, "anos": 3.8},
    "hotel rural extremadura":       {"kwh": 32_500, "eur":  4_958, "kwp": 25, "anos": 3.8},
}

PRODUCCION_MENSUAL_TIPO = [4_100, 5_400, 7_200, 8_600, 9_800, 10_500,
                            10_800, 10_200, 8_400, 6_500, 4_600, 3_800]


# ─── PDF building ─────────────────────────────────────────────────────────────

class PropuestaPDF:

    def __init__(self, lead: dict, ruta: Path):
        self.lead    = lead
        self.pvgis   = lead.get("pvgis") or {}
        self.c       = canvas.Canvas(str(ruta), pagesize=A4)
        self.ruta    = ruta
        self._preparar_datos()

    def _preparar_datos(self):
        sector = self.lead.get("sector", "").lower()
        fb     = AHORROS_ESTIMADOS.get(sector, {"kwh": 50_000, "eur": 7_650, "kwp": 50, "anos": 3.9})

        self.kwp        = self.pvgis.get("kwp_recomendado",    fb["kwp"])
        self.kwh        = self.pvgis.get("kwh_anuales",        fb["kwh"])
        self.eur        = self.pvgis.get("ahorro_anual_eur",   fb["eur"])
        self.anos       = self.pvgis.get("anos_amortizacion",  fb["anos"])
        self.coste      = self.pvgis.get("coste_instalacion_eur", self.kwp * 750)
        self.paneles    = self.pvgis.get("num_paneles", math.ceil(self.kwp * 1000 / 400))
        self.co2        = self.pvgis.get("co2_evitado_kg_ano", round(self.kwh * 0.233, 0))
        self.ahorro_20  = self.pvgis.get("ahorro_neto_20anos_eur", self.eur * 20 - self.coste)

        prod_mensual    = self.pvgis.get("produccion_mensual", [])
        if prod_mensual:
            factor       = self.kwh / sum(m["kwh"] for m in prod_mensual) if prod_mensual else 1
            self.mensual = [round(m["kwh"] * factor, 0) for m in prod_mensual]
        else:
            factor       = self.kwh / sum(PRODUCCION_MENSUAL_TIPO)
            self.mensual = [round(v * factor, 0) for v in PRODUCCION_MENSUAL_TIPO]

        self.tiene_pvgis = bool(self.pvgis)

    # ── Sections ─────────────────────────────────────────────────────────────

    def _cabecera(self):
        c = self.c
        c.setFillColor(AZUL_OSC)
        c.rect(0, PAG_H - 3.2*cm, PAG_W, 3.2*cm, fill=1, stroke=0)

        c.setFillColor(NARANJA)
        c.rect(0, PAG_H - 3.5*cm, PAG_W, 0.3*cm, fill=1, stroke=0)

        c.setFillColor(BLANCO)
        c.setFont("Helvetica-Bold", 22)
        c.drawString(MARGEN, PAG_H - 1.8*cm, "☀ " + EMPRESA_NOMBRE)

        c.setFont("Helvetica", 10)
        c.setFillColor(colors.HexColor("#A8C4E0"))
        c.drawString(MARGEN, PAG_H - 2.5*cm, "Instalaciones fotovoltaicas para empresas en Extremadura")

        c.setFillColor(BLANCO)
        c.setFont("Helvetica", 9)
        c.drawRightString(PAG_W - MARGEN, PAG_H - 1.6*cm, EMPRESA_TELEFONO)
        c.drawRightString(PAG_W - MARGEN, PAG_H - 2.2*cm, EMPRESA_WEB)
        c.drawRightString(PAG_W - MARGEN, PAG_H - 2.8*cm, EMPRESA_EMAIL)

    def _titulo_empresa(self, y: float) -> float:
        c = self.c
        nombre    = self.lead.get("nombre", "")
        direccion = self.lead.get("direccion", "")
        sector    = self.lead.get("sector", "").replace("caceres", "").replace("extremadura", "").strip().title()
        score     = self.lead.get("puntuacion", "")

        c.setFont("Helvetica-Bold", 16)
        c.setFillColor(AZUL_OSC)
        c.drawString(MARGEN, y, "PROPUESTA DE INSTALACIÓN SOLAR FOTOVOLTAICA")

        y -= 0.6*cm
        c.setFont("Helvetica-Bold", 13)
        c.setFillColor(GRIS_OSC)
        c.drawString(MARGEN, y, truncar(nombre, 70))

        y -= 0.5*cm
        c.setFont("Helvetica", 9)
        c.setFillColor(GRIS_MED)
        c.drawString(MARGEN, y, truncar(direccion, 90))

        chip_texto = f"{sector}  |  Potencial solar: {score}/10"
        c.setFont("Helvetica-Bold", 8)
        chip_w = c.stringWidth(chip_texto, "Helvetica-Bold", 8) + 16
        c.setFillColor(GRIS_CLARO)
        c.roundRect(MARGEN, y - 0.55*cm, chip_w, 0.45*cm, 4, fill=1, stroke=0)
        c.setFillColor(AZUL_MED)
        c.drawString(MARGEN + 8, y - 0.38*cm, chip_texto)

        fecha_str = datetime.now().strftime("%d/%m/%Y")
        c.setFont("Helvetica", 8)
        c.setFillColor(GRIS_MED)
        c.drawRightString(PAG_W - MARGEN, y, f"Elaborado el {fecha_str}")
        if not self.tiene_pvgis:
            c.setFillColor(colors.HexColor("#E67E22"))
            c.drawRightString(PAG_W - MARGEN, y - 0.4*cm, "* Estimación sin datos PVGIS")

        return y - 1.2*cm

    def _kpis(self, y: float) -> float:
        c   = self.c
        gap = 0.3*cm
        box_w = (ANCHO - 2*gap) / 3
        box_h = 2.0*cm

        datos_kpi = [
            ("Producción anual",   fmt_kwh(self.kwh),  "☀ kWh generados",        AZUL_MED),
            ("Ahorro anual",       fmt_eur(self.eur),  "€ en factura eléctrica",  VERDE),
            ("Amortización",       f"{self.anos:.1f} años", "retorno de inversión", NARANJA),
        ]

        for idx, (titulo, valor, subtitulo, color_fondo) in enumerate(datos_kpi):
            x = MARGEN + idx * (box_w + gap)

            c.setFillColor(colors.HexColor("#D0D0D0"))
            c.roundRect(x + 2, y - box_h - 2, box_w, box_h, 6, fill=1, stroke=0)

            c.setFillColor(color_fondo)
            c.roundRect(x, y - box_h, box_w, box_h, 6, fill=1, stroke=0)

            c.setFillColor(colors.HexColor("#FFFFFF88") if color_fondo != VERDE else BLANCO)
            c.setFont("Helvetica", 8)
            c.drawCentredString(x + box_w/2, y - 0.55*cm, titulo.upper())

            c.setFillColor(BLANCO)
            c.setFont("Helvetica-Bold", 17 if len(valor) < 12 else 13)
            c.drawCentredString(x + box_w/2, y - 1.25*cm, valor)

            c.setFont("Helvetica", 7)
            c.setFillColor(colors.HexColor("#FFFFFF"))
            c.drawCentredString(x + box_w/2, y - 1.75*cm, subtitulo)

        return y - box_h - 0.6*cm

    def _grafico_mensual(self, y: float) -> float:
        c = self.c

        c.setFillColor(AZUL_OSC)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(MARGEN, y, "PRODUCCIÓN SOLAR MENSUAL ESTIMADA (kWh)")
        c.setStrokeColor(NARANJA)
        c.setLineWidth(1.5)
        c.line(MARGEN, y - 2, MARGEN + ANCHO, y - 2)
        y -= 0.5*cm

        altura_grafico = 4.5*cm
        ancho_grafico  = ANCHO

        meses_cortos = ["Ene","Feb","Mar","Abr","May","Jun",
                        "Jul","Ago","Sep","Oct","Nov","Dic"]
        max_val = max(self.mensual) if self.mensual else 1
        bar_w   = ancho_grafico / 14
        padding = bar_w

        y_base  = y - altura_grafico
        y_top   = y

        c.setStrokeColor(GRIS_CLARO)
        c.setLineWidth(0.5)
        for nivel in [0.25, 0.5, 0.75, 1.0]:
            yg = y_base + nivel * altura_grafico
            c.line(MARGEN, yg, MARGEN + ancho_grafico, yg)

        for i, kwh_mes in enumerate(self.mensual):
            bar_h  = (kwh_mes / max_val) * (altura_grafico - 0.5*cm)
            bx     = MARGEN + padding + i * (ancho_grafico - 2*padding) / 12
            by     = y_base + 0.5*cm
            bw     = (ancho_grafico - 2*padding) / 12 * 0.7

            c.setFillColor(NARANJA)
            c.rect(bx, by, bw, bar_h, fill=1, stroke=0)
            c.setFillColor(NARANJA_OSC)
            c.rect(bx, by, bw * 0.3, bar_h, fill=1, stroke=0)

            c.setFillColor(GRIS_OSC)
            c.setFont("Helvetica-Bold", 6)
            kwh_str = f"{int(kwh_mes/1000):.0f}k" if kwh_mes >= 1000 else str(int(kwh_mes))
            c.drawCentredString(bx + bw/2, by + bar_h + 2, kwh_str)

            c.setFont("Helvetica", 7)
            c.setFillColor(GRIS_MED)
            c.drawCentredString(bx + bw/2, y_base + 2, meses_cortos[i])

        c.setFont("Helvetica", 7)
        c.setFillColor(GRIS_MED)
        c.saveState()
        c.translate(MARGEN - 0.4*cm, y_base + altura_grafico/2)
        c.rotate(90)
        c.drawCentredString(0, 0, "kWh / mes")
        c.restoreState()

        return y_base - 0.3*cm

    def _tabla_economica(self, y: float) -> float:
        c = self.c

        c.setFillColor(AZUL_OSC)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(MARGEN, y, "DESGLOSE ECONÓMICO DE LA INVERSIÓN")
        c.setStrokeColor(NARANJA)
        c.setLineWidth(1.5)
        c.line(MARGEN, y - 2, MARGEN + ANCHO, y - 2)
        y -= 0.5*cm

        col1 = MARGEN
        col2 = MARGEN + ANCHO * 0.55
        col_r = MARGEN + ANCHO
        fila_h = 0.52*cm

        filas = [
            ("Potencia instalada recomendada",  f"{self.kwp} kWp",          None, False),
            ("Número de paneles (400 Wp c/u)",  f"{self.paneles} paneles",  None, False),
            ("Área de tejado necesaria",        f"~{self.kwp * 7:.0f} m²",  None, False),
            ("Producción anual estimada",        fmt_kwh(self.kwh),         None, False),
            ("───────────────────────────",      None,                       None, True),
            ("Coste orientativo de instalación", fmt_eur(self.coste),       None, False),
            ("Ahorro anual en factura",          fmt_eur(self.eur),         None, False),
            ("Años de amortización",            f"{self.anos:.1f} años",    None, False),
            ("Beneficio neto a 20 años",         fmt_eur(self.ahorro_20),   None, False),
            ("───────────────────────────",      None,                       None, True),
            ("CO₂ evitado al año",              f"{self.co2/1000:.1f} toneladas", None, False),
        ]

        for i, (desc, val, _, separador) in enumerate(filas):
            y_fila = y - i * fila_h
            if separador:
                c.setStrokeColor(GRIS_CLARO)
                c.setLineWidth(0.5)
                c.line(col1, y_fila + fila_h * 0.3, col_r, y_fila + fila_h * 0.3)
                continue

            if i % 2 == 0:
                c.setFillColor(GRIS_CLARO)
                c.rect(col1, y_fila - 2, ANCHO, fila_h - 1, fill=1, stroke=0)

            c.setFillColor(GRIS_OSC)
            c.setFont("Helvetica", 9)
            c.drawString(col1 + 4, y_fila + 4, desc)

            if val:
                if "€" in val and float(val.replace(".", "").replace(" €", "").replace(",", "")) > 5000:
                    c.setFillColor(VERDE)
                    c.setFont("Helvetica-Bold", 9)
                else:
                    c.setFillColor(AZUL_MED)
                    c.setFont("Helvetica-Bold", 9)
                c.drawRightString(col_r - 4, y_fila + 4, val)

        return y - len(filas) * fila_h - 0.4*cm

    def _cta(self, y: float) -> float:
        c = self.c

        alto_cta = 1.6*cm
        c.setFillColor(AZUL_OSC)
        c.roundRect(MARGEN, y - alto_cta, ANCHO, alto_cta, 8, fill=1, stroke=0)

        c.setFillColor(NARANJA)
        c.roundRect(MARGEN, y - alto_cta, ANCHO, alto_cta, 8, fill=1, stroke=0)
        c.setFillColor(AZUL_OSC)
        c.rect(MARGEN + 0.5*cm, y - alto_cta, ANCHO - 0.5*cm, alto_cta, fill=1, stroke=0)

        c.setFillColor(BLANCO)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(MARGEN + 0.8*cm, y - 0.65*cm,
                     "¿Le interesa? Solicite su análisis gratuito sin compromiso")
        c.setFont("Helvetica", 9)
        c.drawString(MARGEN + 0.8*cm, y - 1.15*cm,
                     f"📞 {EMPRESA_TELEFONO}   ✉  {EMPRESA_EMAIL}   🌐 {EMPRESA_WEB}")

        return y - alto_cta - 0.3*cm

    def _pie(self, y: float):
        c = self.c
        c.setStrokeColor(GRIS_CLARO)
        c.setLineWidth(0.5)
        c.line(MARGEN, y, PAG_W - MARGEN, y)

        c.setFillColor(GRIS_MED)
        c.setFont("Helvetica", 6.5)
        disclaimer = (
            "* Production data calculated with PVGIS (JRC/European Commission). "
            "Savings are estimates based on an electricity price of 0.18 €/kWh "
            "and a self-consumption rate of 85%. Costs are indicative."
        )
        text_obj = c.beginText(MARGEN, y - 0.35*cm)
        text_obj.setFont("Helvetica", 6.5)
        text_obj.setFillColor(GRIS_MED)
        mid = len(disclaimer) // 2
        corte = disclaimer.rfind(" ", 0, mid + 20)
        text_obj.textLine(disclaimer[:corte])
        text_obj.textLine(disclaimer[corte:].strip())
        c.drawText(text_obj)

    def generar(self):
        c = self.c
        c.setTitle(f"Solar Proposal — {self.lead.get('nombre', '')}")
        c.setAuthor(EMPRESA_NOMBRE)
        c.setSubject("Solar photovoltaic installation proposal")

        y = PAG_H - 3.8*cm   # starting point below the header

        self._cabecera()
        y = self._titulo_empresa(y)
        y -= 0.2*cm
        y = self._kpis(y)
        y -= 0.3*cm
        y = self._grafico_mensual(y)
        y -= 0.4*cm
        y = self._tabla_economica(y)
        y -= 0.4*cm
        y = self._cta(y)
        self._pie(max(y - 0.3*cm, 0.8*cm))

        c.save()


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",         type=int, default=0)
    parser.add_argument("--solo-con-pvgis", action="store_true",
                        help="Skip leads without PVGIS data")
    parser.add_argument("--output",        type=str, default="",
                        help="Output directory (default: data/proposals/)")
    args = parser.parse_args()

    if not INPUT_FILE.exists():
        print(f"ERROR: {INPUT_FILE} not found.")
        print("  Run first: python solar_calculator.py")
        sys.exit(1)

    leads: list[dict] = json.loads(INPUT_FILE.read_text(encoding="utf-8"))

    if args.solo_con_pvgis:
        leads = [l for l in leads if l.get("pvgis")]

    if args.limit:
        leads = leads[:args.limit]

    # Output directory
    out_dir = Path(args.output) if args.output else DATA_DIR / "proposals"
    out_dir.mkdir(parents=True, exist_ok=True)

    total    = len(leads)
    generados = 0
    errores  = 0

    print(f"Generating {total} PDFs in {out_dir}/\n")

    t_inicio = time.time()

    for i, lead in enumerate(leads, 1):
        nombre    = lead.get("nombre", f"lead_{i}")
        place_id  = lead.get("place_id", "")[:8]
        nombre_f  = sanitizar_nombre(nombre)
        ruta_pdf  = out_dir / f"{nombre_f}_{place_id}.pdf"

        try:
            pdf = PropuestaPDF(lead, ruta_pdf)
            pdf.generar()
            generados += 1
            tiene_pvgis = "✓pvgis" if lead.get("pvgis") else " estim"
            print(
                f"\r{barra(i, total, ancho=28)}  {tiene_pvgis}  {nombre[:38]:<38}",
                end="", flush=True,
            )
        except Exception as exc:
            errores += 1
            print(f"\r{barra(i, total, ancho=28)}  [ERROR] {nombre[:38]}: {exc}")

    print()

    total_t  = time.time() - t_inicio
    tam_total = sum(f.stat().st_size for f in out_dir.glob("*.pdf")) / 1024
    print(f"\n{'='*60}")
    print(f"  PDFs generated    : {generados}")
    print(f"  Errors            : {errores}")
    print(f"  Total time        : {total_t:.1f}s  ({total_t/max(generados,1):.2f}s/pdf)")
    print(f"  Total size        : {tam_total:.0f} KB")
    print(f"  Directory         : {out_dir}/")
    print(f"{'='*60}")
    if leads:
        print(f"\n  First PDF: {out_dir}/{sanitizar_nombre(leads[0]['nombre'])}_{leads[0].get('place_id','')[:8]}.pdf")


if __name__ == "__main__":
    main()
