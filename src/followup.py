"""
Automatic follow-up for unanswered emails.

Detects emails with status "enviado" whose fecha_envio exceeds DIAS_SIN_RESPUESTA
days with no registered reply. Sends a second, shorter, more direct email and
updates the status to "seguimiento_enviado".

Usage:
    python followup.py                     # detects and displays candidates
    python followup.py --enviar            # sends the detected follow-ups
    python followup.py --enviar --dias 5   # waits 5 days instead of 3
    python followup.py --dry-run           # simulates without sending
    python followup.py --limit 5           # maximum 5 follow-ups
    python followup.py --force             # no confirmation prompt
"""

import json
import sys
import os
import time
import html
import smtplib
import logging
import argparse
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, formatdate
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = ROOT_DIR / "logs"

sys.path.insert(0, str(ROOT_DIR))
from utils import barra

# ─── Logging ─────────────────────────────────────────────────────────────────

LOG_FILE = LOGS_DIR / "followup.log"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─── Configuration from .env ─────────────────────────────────────────────────

SMTP_HOST      = os.getenv("BREVO_SMTP_HOST",    "smtp-relay.brevo.com")
SMTP_PORT      = int(os.getenv("BREVO_SMTP_PORT", "587"))
SMTP_USER      = os.getenv("BREVO_SMTP_USER",    "")
SMTP_KEY       = os.getenv("BREVO_SMTP_KEY",     "")
FROM_ADDRESS   = os.getenv("EMAIL_FROM_ADDRESS", "")
FROM_NAME      = os.getenv("EMAIL_FROM_NAME",    os.getenv("EMPRESA_NOMBRE", "SolarCáceres"))
EMPRESA_TEL    = os.getenv("EMPRESA_TELEFONO",   "927 000 000")
EMPRESA_WEB    = os.getenv("EMPRESA_WEB",        "www.solarcaceres.es")
EMPRESA_NOMBRE = os.getenv("EMPRESA_NOMBRE",     "SolarCáceres")

ESPERA_ENTRE_ENVIOS = 30
DATA_FILE = DATA_DIR / "generated_emails.json"

# ─── Follow-up template ───────────────────────────────────────────────────────
#
# Shorter and more direct than the initial email.
# References the first contact without being pushy.

ASUNTO_SEGUIMIENTO = "Follow-up: free solar analysis for {nombre_corto}"

CUERPO_SEGUIMIENTO = """Last week we sent you a proposal for a free solar analysis for {nombre_corto}.

We know how busy day-to-day can get. We just wanted to confirm you received our message and that the offer still stands: a personalised study at no cost or commitment showing exactly how much you could save on electricity.

Do you have 10 minutes this week? Call us at {telefono} or simply reply to this email.

Best regards,
The {empresa} team
{web}"""


# ─── Utilities ───────────────────────────────────────────────────────────────

def _nombre_corto_desde_asunto(asunto: str) -> str:
    """Extracts the short name from the original subject: '... — Name, Municipality'"""
    if "—" in asunto:
        parte = asunto.split("—", 1)[1].strip()
        # Remove municipality if present
        if "," in parte:
            parte = parte.split(",")[0].strip()
        return parte
    return ""


def _parsear_fecha(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        # Python 3.10 and earlier compatibility
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def candidatos_seguimiento(registros: list[dict], dias: int) -> list[dict]:
    ahora  = datetime.now(timezone.utc)
    umbral = ahora - timedelta(days=dias)
    cola   = []
    for r in registros:
        if r.get("estado_envio") not in ("enviado",):
            continue
        if r.get("respuesta") is not None:
            continue
        if not r.get("email_contacto"):
            continue
        fecha_envio = _parsear_fecha(r.get("fecha_envio"))
        if fecha_envio and fecha_envio < umbral:
            cola.append(r)
    return cola


# ─── SMTP ─────────────────────────────────────────────────────────────────────

def construir_seguimiento(registro: dict) -> MIMEMultipart:
    nombre_corto = _nombre_corto_desde_asunto(registro.get("asunto", ""))
    if not nombre_corto:
        nombre_corto = registro.get("nombre", "your company")[:30]

    asunto = ASUNTO_SEGUIMIENTO.format(nombre_corto=nombre_corto)
    cuerpo = CUERPO_SEGUIMIENTO.format(
        nombre_corto = nombre_corto,
        telefono     = EMPRESA_TEL,
        empresa      = EMPRESA_NOMBRE,
        web          = EMPRESA_WEB,
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"]    = asunto
    msg["From"]       = formataddr((FROM_NAME, FROM_ADDRESS))
    msg["To"]         = registro["email_contacto"]
    msg["Date"]       = formatdate(localtime=True)
    # Thread: reference the original email so it appears as a reply
    if registro.get("id"):
        msg["In-Reply-To"] = f"<{registro['id']}@solarcaceres.es>"
        msg["References"]  = f"<{registro['id']}@solarcaceres.es>"

    parte_texto = MIMEText(cuerpo, "plain", "utf-8")
    cuerpo_html = html.escape(cuerpo).replace("\n", "<br>")
    parte_html  = MIMEText(
        f"<html><body style='font-family:Arial,sans-serif;font-size:14px;color:#222;'>"
        f"{cuerpo_html}</body></html>",
        "html", "utf-8",
    )
    msg.attach(parte_texto)
    msg.attach(parte_html)

    # Store subject and body in the record for traceability
    registro["_asunto_seguimiento"] = asunto
    registro["_cuerpo_seguimiento"] = cuerpo
    return msg


def enviar_smtp(msg: MIMEMultipart, destinatario: str) -> None:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_USER, SMTP_KEY)
        server.sendmail(FROM_ADDRESS, [destinatario], msg.as_string())


# ─── Atomic persistence ───────────────────────────────────────────────────────

def guardar_datos(registros: list[dict]) -> None:
    contenido = json.dumps(registros, ensure_ascii=False, indent=2)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(contenido, encoding="utf-8")
    tmp.replace(DATA_FILE)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Follow-up for unanswered emails")
    parser.add_argument("--enviar",  action="store_true", help="Send follow-ups (default: display only)")
    parser.add_argument("--dias",    type=int, default=3,  help="Days without reply to trigger follow-up (def: 3)")
    parser.add_argument("--dry-run", action="store_true",  help="Simulate without sending (implies --enviar)")
    parser.add_argument("--limit",   type=int, default=0,  help="Maximum follow-ups to send")
    parser.add_argument("--force",   action="store_true",  help="No confirmation prompt")
    args = parser.parse_args()

    if args.dry_run:
        args.enviar = True

    # Validate config if sending
    if args.enviar and not args.dry_run:
        fallos = []
        if not SMTP_USER:    fallos.append("BREVO_SMTP_USER")
        if not SMTP_KEY:     fallos.append("BREVO_SMTP_KEY")
        if not FROM_ADDRESS: fallos.append("EMAIL_FROM_ADDRESS")
        if fallos:
            log.error(f"Variables not configured in .env: {', '.join(fallos)}")
            sys.exit(1)

    registros: list[dict] = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    cola = candidatos_seguimiento(registros, args.dias)

    if args.limit:
        cola = cola[:args.limit]

    total = len(cola)

    # ── Detection report ──────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  Total sent emails          : {sum(1 for r in registros if r.get('estado_envio')=='enviado')}")
    print(f"  Without reply > {args.dias} days    : {total}")
    if total == 0:
        print(f"\n  No candidates for follow-up. Check back later.")
        print(f"{'─'*60}\n")
        return

    print(f"\n  Follow-up candidates:")
    for r in cola[:10]:
        fecha = _parsear_fecha(r.get("fecha_envio"))
        dias_pasados = (datetime.now(timezone.utc) - fecha).days if fecha else "?"
        print(f"    [{dias_pasados}d]  {r['nombre'][:42]:<42}  {r['email_contacto']}")
    if total > 10:
        print(f"    ... and {total-10} more")

    if not args.enviar:
        print(f"\n  (To send follow-ups, run with --enviar)")
        print(f"{'─'*60}\n")
        return

    # ── Confirmation ──────────────────────────────────────────────────────────
    tiempo_est = total * (ESPERA_ENTRE_ENVIOS + 2)
    print(f"\n  Mode    : {'DRY-RUN' if args.dry_run else 'REAL'}")
    print(f"  Time    : ~{tiempo_est//60}m {tiempo_est%60}s")
    print(f"{'─'*60}\n")

    if not args.force and not args.dry_run:
        resp = input(f"Confirm sending {total} follow-ups? [y/N] ").strip().lower()
        if resp != "y":
            print("Cancelled.")
            return

    # ── Send ──────────────────────────────────────────────────────────────────
    enviados    = 0
    errores_env = 0
    t_inicio    = time.time()

    for i, registro in enumerate(cola, 1):
        nombre = registro.get("nombre", "")[:28]
        dest   = registro["email_contacto"]

        try:
            msg = construir_seguimiento(registro)
            asunto_seg = registro.get("_asunto_seguimiento", "")

            if not args.dry_run:
                enviar_smtp(msg, dest)

            # Update original record
            for r in registros:
                if r["id"] == registro["id"]:
                    r["estado_envio"]       = "seguimiento_enviado" if not args.dry_run else "dry-run-seg"
                    r["fecha_seguimiento"]  = datetime.now(timezone.utc).isoformat()
                    r["asunto_seguimiento"] = asunto_seg
                    r.pop("_asunto_seguimiento", None)
                    r.pop("_cuerpo_seguimiento", None)
                    break

            enviados += 1
            modo_str = "[DRY]" if args.dry_run else "[ OK]"
            log.info(f"{modo_str} follow-up → {dest}")

        except smtplib.SMTPRecipientsRefused:
            for r in registros:
                if r["id"] == registro["id"]:
                    r["estado_envio"]  = "error_bounce"
                    r["error_detalle"] = "recipient refused in follow-up"
                    break
            errores_env += 1
            log.warning(f"[BOUNCE] {dest}")

        except Exception as exc:
            for r in registros:
                if r["id"] == registro["id"]:
                    r["estado_envio"]  = "error"
                    r["error_detalle"] = f"follow-up: {str(exc)[:100]}"
                    break
            errores_env += 1
            log.error(f"[ERROR] {dest}: {exc}")

        guardar_datos(registros)

        transcurrido = time.time() - t_inicio
        vel = i / transcurrido if transcurrido > 0 else 1
        eta = int((total - i) * (ESPERA_ENTRE_ENVIOS + 1.0 / vel))
        print(
            f"\r{barra(i, total, ancho=28)}  {nombre:<28}  ETA {eta//60}m{eta%60:02d}s",
            end="", flush=True,
        )

        if i < total:
            if not args.dry_run:
                time.sleep(ESPERA_ENTRE_ENVIOS)
            else:
                time.sleep(0.05)

    print()
    total_tiempo = time.time() - t_inicio
    print(f"\n{'='*60}")
    print(f"  Follow-ups OK   : {enviados}")
    print(f"  Errors/Bounces  : {errores_env}")
    print(f"  Total time      : {int(total_tiempo//60)}m {int(total_tiempo%60)}s")
    print(f"  Log saved to    : {LOG_FILE.name}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
