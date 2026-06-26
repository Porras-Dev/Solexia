"""
Cold email sending via Brevo SMTP.

Reads generated_emails.json, sends those with status "pendiente" and a valid
email_contacto, and updates the status after each send.

Usage:
    python email_sender.py               # shows summary and asks for confirmation
    python email_sender.py --dry-run     # simulates without sending
    python email_sender.py --limit 10    # sends only the first 10
    python email_sender.py --force       # no confirmation prompt
    python email_sender.py --id <uuid>   # retries a specific email by id

Required in .env:
    BREVO_SMTP_HOST, BREVO_SMTP_PORT, BREVO_SMTP_USER,
    BREVO_SMTP_KEY, EMAIL_FROM_ADDRESS, EMAIL_FROM_NAME
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
from datetime import datetime, timezone
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

LOG_FILE = LOGS_DIR / "emails.log"
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

ESPERA_ENTRE_ENVIOS = 30   # seconds
DATA_FILE = DATA_DIR / "generated_emails.json"


# ─── Configuration validation ─────────────────────────────────────────────────

def validar_config() -> list[str]:
    errores = []
    if not SMTP_USER:
        errores.append("BREVO_SMTP_USER not configured in .env")
    if not SMTP_KEY:
        errores.append("BREVO_SMTP_KEY not configured in .env")
    if not FROM_ADDRESS:
        errores.append("EMAIL_FROM_ADDRESS not configured in .env")
    elif "@" not in FROM_ADDRESS:
        errores.append(f"EMAIL_FROM_ADDRESS does not look like a valid email: {FROM_ADDRESS!r}")
    return errores


# ─── SMTP ─────────────────────────────────────────────────────────────────────

def construir_mensaje(registro: dict) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = registro["asunto"]
    msg["From"]    = formataddr((FROM_NAME, FROM_ADDRESS))
    msg["To"]      = registro["email_contacto"]
    msg["Date"]    = formatdate(localtime=True)
    msg["Message-ID"] = f"<{registro['id']}@solarcaceres.es>"

    # Plain text
    parte_texto = MIMEText(registro["cuerpo"], "plain", "utf-8")

    # HTML — escape entities before inserting in the DOM
    cuerpo_html = html.escape(registro["cuerpo"]).replace("\n", "<br>")
    parte_html = MIMEText(
        f"<html><body style='font-family:Arial,sans-serif;font-size:14px;color:#222;'>"
        f"{cuerpo_html}</body></html>",
        "html", "utf-8",
    )

    msg.attach(parte_texto)
    msg.attach(parte_html)
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
    """Writes the JSON atomically (temp + rename) to prevent corruption."""
    contenido = json.dumps(registros, ensure_ascii=False, indent=2)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(contenido, encoding="utf-8")
    tmp.replace(DATA_FILE)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Email sending via Brevo SMTP")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without sending")
    parser.add_argument("--limit",   type=int, default=0, help="Maximum emails to send")
    parser.add_argument("--force",   action="store_true", help="No confirmation prompt")
    parser.add_argument("--id",      type=str, default="", help="UUID of the email to resend")
    args = parser.parse_args()

    # Validate credentials before continuing
    errores = validar_config()
    if errores and not args.dry_run:
        for e in errores:
            log.error(f"Config: {e}")
        sys.exit(1)

    # Load data
    registros: list[dict] = json.loads(DATA_FILE.read_text(encoding="utf-8"))

    # Filter candidates
    if args.id:
        cola = [r for r in registros if r["id"] == args.id]
        if not cola:
            log.error(f"Id not found: {args.id}")
            sys.exit(1)
    else:
        cola = [
            r for r in registros
            if r.get("estado_envio") == "pendiente"
            and r.get("email_contacto")
        ]

    if args.limit:
        cola = cola[:args.limit]

    total = len(cola)
    if total == 0:
        log.info("No pending emails with a valid contact address.")
        return

    # Pre-send summary
    tiempo_est = total * (ESPERA_ENTRE_ENVIOS + 2)
    print(f"\n{'─'*60}")
    print(f"  Emails a enviar       : {total}")
    print(f"  Tiempo estimado       : ~{tiempo_est//60}m {tiempo_est%60}s")
    print(f"  Espera entre envíos   : {ESPERA_ENTRE_ENVIOS}s")
    print(f"  Remitente             : {FROM_NAME} <{FROM_ADDRESS}>")
    print(f"  SMTP                  : {SMTP_HOST}:{SMTP_PORT}")
    print(f"  Modo                  : {'DRY-RUN (sin envío real)' if args.dry_run else 'REAL'}")
    print(f"{'─'*60}")
    print(f"\nPRIMER EMAIL DE MUESTRA:")
    print(f"  Para:   {cola[0]['email_contacto']}")
    print(f"  Asunto: {cola[0]['asunto']}")
    print(f"  Cuerpo: {cola[0]['cuerpo'][:200]}...")
    print()

    if not args.force and not args.dry_run:
        resp = input(f"¿Confirmas el envío de {total} emails? [s/N] ").strip().lower()
        if resp != "s":
            print("Cancelado.")
            return

    # Send
    enviados = 0
    errores_envio = 0
    t_inicio = time.time()

    for i, registro in enumerate(cola, 1):
        nombre = registro.get("nombre", "")[:28]
        dest   = registro["email_contacto"]

        try:
            if not args.dry_run:
                msg = construir_mensaje(registro)
                enviar_smtp(msg, dest)

            # Update status in original list
            for r in registros:
                if r["id"] == registro["id"]:
                    r["estado_envio"]  = "enviado" if not args.dry_run else "dry-run"
                    r["fecha_envio"]   = datetime.now(timezone.utc).isoformat()
                    r["error_detalle"] = None
                    break

            enviados += 1
            estado_str = "[DRY]" if args.dry_run else "[ OK]"
            log.info(f"{estado_str} → {dest} | {registro['asunto'][:55]}")

        except smtplib.SMTPRecipientsRefused:
            for r in registros:
                if r["id"] == registro["id"]:
                    r["estado_envio"]  = "error_bounce"
                    r["error_detalle"] = "recipient refused"
                    break
            errores_envio += 1
            log.warning(f"[BOUNCE] {dest}")

        except smtplib.SMTPException as exc:
            for r in registros:
                if r["id"] == registro["id"]:
                    r["estado_envio"]  = "error"
                    r["error_detalle"] = str(exc)[:120]
                    break
            errores_envio += 1
            log.error(f"[ERROR] {dest}: {exc}")

        except Exception as exc:
            for r in registros:
                if r["id"] == registro["id"]:
                    r["estado_envio"]  = "error"
                    r["error_detalle"] = str(exc)[:120]
                    break
            errores_envio += 1
            log.error(f"[ERROR] {dest}: {exc}")

        # Save state after each send
        guardar_datos(registros)

        # Progress
        transcurrido = time.time() - t_inicio
        vel = i / transcurrido if transcurrido > 0 else 1
        eta = int((total - i) * (ESPERA_ENTRE_ENVIOS + 1.0 / vel))
        print(
            f"\r{barra(i, total, ancho=28)}  {nombre:<28}  ETA {eta//60}m{eta%60:02d}s",
            end="", flush=True,
        )

        # Wait between sends (except the last)
        if i < total:
            if not args.dry_run:
                time.sleep(ESPERA_ENTRE_ENVIOS)
            else:
                time.sleep(0.05)

    print()
    total_tiempo = time.time() - t_inicio
    print(f"\n{'='*60}")
    print(f"  Enviados OK     : {enviados}")
    print(f"  Errores/Bounces : {errores_envio}")
    print(f"  Tiempo total    : {int(total_tiempo//60)}m {int(total_tiempo%60)}s")
    print(f"  Log guardado en : {LOG_FILE.name}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
