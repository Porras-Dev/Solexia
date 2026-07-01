"""
Calendar management with Google Calendar API.

Usage modes:
  1. Propose free slots for a company:
       python calendar_manager.py --proponer --nombre "Cooperativa San Isidro"

  2. Create the event when the lead confirms a slot:
       python calendar_manager.py --crear --nombre "Cooperativa San Isidro" --inicio "2024-01-15T10:00"

  3. List upcoming solar meetings:
       python calendar_manager.py --listar [--dias 14]

Typical flow from n8n / manual:
  → lead replies to email
  → run --proponer → send 3 options to lead
  → lead chooses one
  → run --crear → event in Calendar + Telegram notification

Required in .env:
    GOOGLE_CALENDAR_CREDENTIALS_FILE  path to OAuth2 credentials JSON
    GOOGLE_CALENDAR_TOKEN_FILE        path to store the token (auto-generated)
    GOOGLE_CALENDAR_ID                "primary" or calendar email
    TELEGRAM_BOT_TOKEN                Telegram bot token
    TELEGRAM_CHAT_ID                  chat/group ID to send notifications to

Install dependencies:
    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
"""

import json
import sys
import os
import argparse
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone, date
from dotenv import load_dotenv
import requests

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"

# ─── Google imports (clear error if not installed) ────────────────────────────

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("ERROR: Google libraries not installed.")
    print("  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
    sys.exit(1)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        print("ERROR: zoneinfo not available. Install backports.zoneinfo or use Python 3.9+")
        sys.exit(1)

# ─── Configuration from .env ─────────────────────────────────────────────────

CREDENTIALS_FILE  = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_FILE",
                               str(ROOT_DIR / "credentials.json"))
TOKEN_FILE        = os.getenv("GOOGLE_CALENDAR_TOKEN_FILE",
                               str(ROOT_DIR / "token_calendar.json"))
CALENDAR_ID       = os.getenv("GOOGLE_CALENDAR_ID",               "primary")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN",               "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID",                 "")
GOOGLE_AUTH_EMAIL = os.getenv("GOOGLE_AUTH_EMAIL",                "")

EMPRESA_NOMBRE   = os.getenv("EMPRESA_NOMBRE",   "SolarCáceres")
EMPRESA_TELEFONO = os.getenv("EMPRESA_TELEFONO", "927 000 000")
EMPRESA_WEB      = os.getenv("EMPRESA_WEB",      "www.solarcaceres.es")

TZ_MADRID = ZoneInfo("Europe/Madrid")

# Calendar parameters
HORA_INICIO_LABORAL = 9    # 09:00
HORA_FIN_LABORAL    = 18   # 18:00
DURACION_REUNION_H  = 1    # hours
FRANJAS_A_PROPONER  = 3

EMAILS_FILE    = DATA_DIR / "generated_emails.json"
PVGIS_FILE     = DATA_DIR / "leads_with_solar_data.json"
PROPUESTAS_DIR = DATA_DIR / "proposals"

SCOPES = ["https://www.googleapis.com/auth/calendar"]


# ─── Google Calendar authentication ──────────────────────────────────────────

def _oauth_flow_manual(creds_path: Path, port: int = 8080) -> "Credentials":
    """
    Custom OAuth2 flow without CSRF state verification.
    Avoids MismatchingStateError when the browser reuses a cached
    response from a previous attempt.
    """
    import webbrowser
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse, parse_qs
    import requests as _req

    config = json.loads(creds_path.read_text(encoding="utf-8"))["installed"]
    redirect_uri = f"http://localhost:{port}/"

    # Build authorisation URL manually (no PKCE for simplicity)
    from urllib.parse import urlencode
    params = {
        "response_type":   "code",
        "client_id":       config["client_id"],
        "redirect_uri":    redirect_uri,
        "scope":           " ".join(SCOPES),
        "access_type":     "offline",
        "prompt":          "consent",
    }
    auth_url = config["auth_uri"] + "?" + urlencode(params)

    print(f"\n{'='*62}")
    print(f"  GOOGLE CALENDAR AUTHORISATION")
    print(f"{'='*62}")
    print(f"\n  Open this link in your browser:")
    print(f"\n  {auth_url}\n")
    cuenta = GOOGLE_AUTH_EMAIL or "your Google account"
    print(f"  Sign in with {cuenta}")
    print(f"  and accept the Google Calendar permissions.")
    print(f"\n  Waiting for response (max 5 minutes)...")
    print(f"{'='*62}\n")

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # Minimal HTTP server that captures the authorisation code
    auth_code: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            code = qs.get("code", [None])[0]
            if code:
                auth_code.append(code)
                body = b"<h2>Authorisation complete. You can close this tab.</h2>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                error = qs.get("error", ["unknown"])[0]
                body = f"<h2>Error: {error}</h2>".encode()
                self.send_response(400)
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *_):
            pass  # silence server logs

    server = HTTPServer(("localhost", port), _Handler)
    server.timeout = 300
    server.handle_request()   # waits for ONE request then stops

    if not auth_code:
        raise RuntimeError("No authorisation code received from Google.")

    # Exchange code for tokens via direct POST
    token_resp = _req.post(config["token_uri"], data={
        "code":          auth_code[0],
        "client_id":     config["client_id"],
        "client_secret": config["client_secret"],
        "redirect_uri":  redirect_uri,
        "grant_type":    "authorization_code",
    }, timeout=15)
    token_resp.raise_for_status()
    td = token_resp.json()

    from google.oauth2.credentials import Credentials as _Creds
    return _Creds(
        token         = td["access_token"],
        refresh_token = td.get("refresh_token"),
        token_uri     = config["token_uri"],
        client_id     = config["client_id"],
        client_secret = config["client_secret"],
        scopes        = SCOPES,
    )


def obtener_servicio() -> object:
    """Authenticates with Google Calendar and returns the service. Opens browser on first use."""
    creds = None
    token_path = Path(TOKEN_FILE)
    creds_path = Path(CREDENTIALS_FILE)

    if not creds_path.exists():
        print(f"ERROR: {CREDENTIALS_FILE} not found")
        print("  1. Go to https://console.cloud.google.com → APIs & Services → Credentials")
        print("  2. Create OAuth 2.0 credential (type: Desktop application)")
        print("  3. Download the JSON and place it as 'credentials.json' in the project root")
        print(f"  4. Add GOOGLE_CALENDAR_CREDENTIALS_FILE={CREDENTIALS_FILE} to .env")
        sys.exit(1)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = _oauth_flow_manual(creds_path)

        token_path.write_text(creds.to_json(), encoding="utf-8")
        print(f"\n✓ Token saved to {TOKEN_FILE}")
        print(f"  (Next time authorisation will not be requested again)\n")

    return build("calendar", "v3", credentials=creds)


# ─── Free slot search ────────────────────────────────────────────────────────

def _periodos_ocupados(service, desde: datetime, hasta: datetime) -> list[dict]:
    """Returns list of {start, end} in UTC for busy periods."""
    body = {
        "timeMin": desde.isoformat(),
        "timeMax": hasta.isoformat(),
        "items":   [{"id": CALENDAR_ID}],
        "timeZone": "Europe/Madrid",
    }
    resultado = service.freebusy().query(body=body).execute()
    return resultado["calendars"][CALENDAR_ID].get("busy", [])


def _es_dia_laboral(d: date) -> bool:
    return d.weekday() < 5   # monday=0 … friday=4


def proponer_franjas(service, dias: int = 7) -> list[dict]:
    """
    Returns up to FRANJAS_A_PROPONER free slots in the next `dias` working days.
    Each slot is a dict with 'inicio' and 'fin' as datetime (Europe/Madrid).
    """
    ahora  = datetime.now(TZ_MADRID)
    fin_bq = ahora + timedelta(days=dias + 2)   # +2 days buffer for weekends

    ocupados_raw = _periodos_ocupados(service, ahora.astimezone(timezone.utc),
                                      fin_bq.astimezone(timezone.utc))

    # Convert busy periods to Madrid TZ
    ocupados = []
    for p in ocupados_raw:
        ini = datetime.fromisoformat(p["start"].replace("Z", "+00:00")).astimezone(TZ_MADRID)
        fin = datetime.fromisoformat(p["end"].replace("Z", "+00:00")).astimezone(TZ_MADRID)
        ocupados.append((ini, fin))

    # Generate hour-by-hour candidates on working days
    candidatos: list[datetime] = []
    dia_actual = ahora.date()
    dias_revisados = 0

    while len(candidatos) < 50 and dias_revisados < 20:
        if _es_dia_laboral(dia_actual):
            for hora in range(HORA_INICIO_LABORAL, HORA_FIN_LABORAL - DURACION_REUNION_H + 1):
                slot_ini = datetime(dia_actual.year, dia_actual.month, dia_actual.day,
                                    hora, 0, tzinfo=TZ_MADRID)
                slot_fin = slot_ini + timedelta(hours=DURACION_REUNION_H)

                # Skip slots in the past or within the next hour
                if slot_ini < ahora + timedelta(hours=2):
                    continue

                # Verify no overlap with any busy period
                libre = all(
                    slot_fin <= ini or slot_ini >= fin
                    for ini, fin in ocupados
                )
                if libre:
                    candidatos.append(slot_ini)

        dia_actual += timedelta(days=1)
        dias_revisados += 1

    # Select FRANJAS_A_PROPONER well-distributed slots
    # Prefer 10:00, 11:00, 16:00 and spread across different days
    HORAS_PREFERIDAS = [10, 11, 16, 9, 15, 17, 12, 14]
    seleccionados: list[datetime] = []
    dias_usados: set[date] = set()

    # First pass: preferred hours
    for hora_pref in HORAS_PREFERIDAS:
        if len(seleccionados) >= FRANJAS_A_PROPONER:
            break
        for c in candidatos:
            if c.hour == hora_pref and c.date() not in dias_usados:
                seleccionados.append(c)
                dias_usados.add(c.date())
                break

    # If not enough, take the first available
    for c in candidatos:
        if len(seleccionados) >= FRANJAS_A_PROPONER:
            break
        if c not in seleccionados:
            seleccionados.append(c)

    return [
        {
            "inicio": s,
            "fin":    s + timedelta(hours=DURACION_REUNION_H),
            "inicio_iso": s.isoformat(),
            "fin_iso":    (s + timedelta(hours=DURACION_REUNION_H)).isoformat(),
            "label": s.strftime("%A %d de %B a las %H:%M").capitalize(),
        }
        for s in sorted(seleccionados)
    ]


# ─── Google Calendar event creation ──────────────────────────────────────────

def _buscar_pdf(nombre: str) -> Path | None:
    """Finds the proposal PDF closest to the company name."""
    if not PROPUESTAS_DIR.exists():
        return None
    nombre_slug = re.sub(r"[^\w\s]", "", nombre.lower())
    nombre_slug = re.sub(r"\s+", "_", nombre_slug.strip())[:30]
    for pdf in PROPUESTAS_DIR.glob("*.pdf"):
        if nombre_slug[:12] in pdf.name.lower():
            return pdf
    return None


def _descripcion_evento(empresa: dict, pvgis: dict | None) -> str:
    lineas = [
        f"🏢 COMPANY: {empresa.get('nombre', '')}",
        f"📍 Address: {empresa.get('direccion', 'N/A')}",
        f"📞 Phone: {empresa.get('telefono', 'N/A')}",
        f"✉ Email: {empresa.get('email_contacto') or empresa.get('email', 'N/A')}",
        f"🏷 Sector: {empresa.get('sector', '')}  |  Score: {empresa.get('puntuacion', '')}/10",
        "",
        "─── SOLAR ANALYSIS (PVGIS) ───",
    ]
    if pvgis:
        lineas += [
            f"⚡ Annual production:  {pvgis['kwh_anuales']:,.0f} kWh".replace(",", "."),
            f"💶 Annual savings:     {pvgis['ahorro_anual_eur']:,.0f} €".replace(",", "."),
            f"📦 Installation (kWp): {pvgis['kwp_recomendado']} kWp  ({pvgis['num_paneles']} panels)",
            f"💰 Indicative cost:    {pvgis['coste_instalacion_eur']:,.0f} €".replace(",", "."),
            f"📅 Payback:            {pvgis['anos_amortizacion']} years",
            f"🌿 CO₂ avoided:       {pvgis['co2_evitado_kg_ano']/1000:.1f} t/year",
        ]
    else:
        lineas.append("  (PVGIS data not available)")

    pdf = _buscar_pdf(empresa.get("nombre", ""))
    if pdf:
        lineas += ["", f"📄 Proposal PDF: {pdf}"]

    lineas += [
        "",
        "─── MEETING PREPARATION ───",
        "• Bring printed PDF proposal",
        "• Review client's recent electricity bill",
        "• Confirm roof availability",
        "",
        f"Generated by {EMPRESA_NOMBRE} — {EMPRESA_WEB}",
    ]
    return "\n".join(lineas)


def crear_evento(service, empresa: dict, pvgis: dict | None,
                 inicio: datetime) -> dict:
    """Creates the event in Google Calendar and returns the created event."""
    fin = inicio + timedelta(hours=DURACION_REUNION_H)
    nombre = empresa.get("nombre", "Unknown company")

    evento = {
        "summary":     f"☀ Solar meeting — {nombre}",
        "description": _descripcion_evento(empresa, pvgis),
        "start":       {"dateTime": inicio.isoformat(), "timeZone": "Europe/Madrid"},
        "end":         {"dateTime": fin.isoformat(),    "timeZone": "Europe/Madrid"},
        "colorId":     "5",   # yellow/banana (solar)
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email",  "minutes": 24 * 60},  # email the day before
                {"method": "popup",  "minutes": 30},        # alert 30 min before
            ],
        },
    }

    # Add lead as attendee if they have a real email
    email_lead = empresa.get("email_contacto") or empresa.get("email")
    if email_lead and "@" in email_lead and "facebook" not in email_lead:
        evento["attendees"] = [{"email": email_lead, "displayName": nombre}]

    return service.events().insert(
        calendarId=CALENDAR_ID,
        body=evento,
        sendUpdates="none",   # do not send automatic invitation to lead
    ).execute()


# ─── Telegram notification ────────────────────────────────────────────────────

def enviar_telegram(mensaje: str) -> bool:
    """Sends message to the operator via Telegram Bot API. Returns True if OK."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("  ⚠ Telegram not configured — skipping notification")
        print("    Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"  ⚠ Telegram error: {exc}")
        return False


def _mensaje_telegram(empresa: dict, pvgis: dict | None, evento: dict, inicio: datetime) -> str:
    nombre  = empresa.get("nombre", "")
    dir_    = empresa.get("direccion", "")[:60]
    tel     = empresa.get("telefono", "N/A")
    email   = empresa.get("email_contacto") or empresa.get("email") or "N/A"
    sector  = empresa.get("sector", "")
    score   = empresa.get("puntuacion", "")
    enlace  = evento.get("htmlLink", "")

    meses_es = ["Jan","Feb","Mar","Apr","May","Jun",
                "Jul","Aug","Sep","Oct","Nov","Dec"]
    fecha_str = (f"{inicio.strftime('%A').capitalize()} "
                 f"{inicio.day} {meses_es[inicio.month-1]} {inicio.year} "
                 f"at {inicio.strftime('%H:%M')}")

    lineas = [
        "🌞 <b>NEW SOLAR MEETING CONFIRMED</b>",
        "",
        f"🏢 <b>Company:</b> {nombre}",
        f"📍 <b>Address:</b> {dir_}",
        f"📞 <b>Phone:</b> {tel}",
        f"✉ <b>Email:</b> {email}",
        f"🏷 <b>Sector:</b> {sector} | Potential: {score}/10",
        "",
        f"📅 <b>Date:</b> {fecha_str}",
        f"⏱ <b>Duration:</b> {DURACION_REUNION_H} hour(s)",
        "",
    ]

    if pvgis:
        kwh  = f"{pvgis['kwh_anuales']:,.0f}".replace(",", ".")
        eur  = f"{pvgis['ahorro_anual_eur']:,.0f}".replace(",", ".")
        anos = pvgis["anos_amortizacion"]
        lineas += [
            "☀ <b>Solar analysis:</b>",
            f"  • Production: {kwh} kWh/year",
            f"  • Savings: {eur} €/year",
            f"  • Payback: {anos} years",
            "",
        ]

    pdf = _buscar_pdf(nombre)
    if pdf:
        lineas.append(f"📄 <b>PDF:</b> {pdf.name}")
    if enlace:
        lineas.append(f"🗓 <b>Event:</b> <a href='{enlace}'>View in Google Calendar</a>")

    lineas += ["", f"— {EMPRESA_NOMBRE}"]
    return "\n".join(lineas)


# ─── Load company data ────────────────────────────────────────────────────────

def _cargar_empresa_por_nombre(nombre_buscado: str) -> tuple[dict | None, dict | None]:
    """
    Searches for a company by name in generated_emails.json and leads_with_solar_data.json.
    Returns (email_data, pvgis_data). Either may be None if not found.
    """
    nombre_lower = nombre_buscado.lower().strip()

    empresa_email = None
    if EMAILS_FILE.exists():
        emails = json.loads(EMAILS_FILE.read_text(encoding="utf-8"))
        for e in emails:
            if nombre_lower in e.get("nombre", "").lower():
                empresa_email = e
                break

    empresa_pvgis = None
    pvgis_data    = None
    if PVGIS_FILE.exists():
        leads = json.loads(PVGIS_FILE.read_text(encoding="utf-8"))
        for lead in leads:
            if nombre_lower in lead.get("nombre", "").lower():
                empresa_pvgis = lead
                pvgis_data    = lead.get("pvgis")
                break

    # Merge: pvgis has more fields (direccion, lat, lng, etc.)
    empresa = {**(empresa_pvgis or {}), **(empresa_email or {})}
    return empresa if empresa else None, pvgis_data


# ─── Main commands ────────────────────────────────────────────────────────────

def cmd_proponer(args):
    service = obtener_servicio()
    empresa, pvgis = _cargar_empresa_por_nombre(args.nombre)

    if not empresa:
        print(f"⚠ Company not found: '{args.nombre}'")
        print("  Check generated_emails.json or leads_with_solar_data.json")
        sys.exit(1)

    print(f"\nSearching free slots for: {empresa.get('nombre', '')}")
    print(f"Calendar: {CALENDAR_ID}  |  Horizon: {args.dias} days\n")

    franjas = proponer_franjas(service, dias=args.dias)

    if not franjas:
        print("No free slots found. Check your calendar.")
        sys.exit(0)

    print(f"{'─'*55}")
    print(f"  3 AVAILABLE SLOTS FOR THE MEETING")
    print(f"{'─'*55}")
    for i, f in enumerate(franjas, 1):
        print(f"  Option {i}: {f['label']}")
        print(f"            {f['inicio_iso']}  →  {f['fin_iso']}")
        print()

    print("To confirm a slot, run:")
    print(f'  python calendar_manager.py --crear --nombre "{args.nombre}" --inicio "YYYY-MM-DDTHH:MM"')


def cmd_crear(args):
    service = obtener_servicio()
    empresa, pvgis = _cargar_empresa_por_nombre(args.nombre)

    if not empresa:
        print(f"⚠ Company not found: '{args.nombre}'")
        sys.exit(1)

    # Parse the start datetime
    try:
        inicio = datetime.fromisoformat(args.inicio)
        if inicio.tzinfo is None:
            inicio = inicio.replace(tzinfo=TZ_MADRID)
    except ValueError:
        print(f"⚠ Invalid --inicio format: '{args.inicio}'")
        print("  Use ISO format: 2024-01-15T10:00  or  2024-01-15T10:00:00+01:00")
        sys.exit(1)

    nombre = empresa.get("nombre", "")
    print(f"\nCreating event for: {nombre}")
    print(f"Start: {inicio.strftime('%A %d/%m/%Y at %H:%M')}")

    try:
        evento = crear_evento(service, empresa, pvgis, inicio)
        enlace = evento.get("htmlLink", "")
        print(f"\n✓ Event created: {enlace}")

        # Telegram notification
        msg = _mensaje_telegram(empresa, pvgis, evento, inicio)
        ok = enviar_telegram(msg)
        if ok:
            print("✓ Telegram notification sent")

    except HttpError as exc:
        print(f"✗ Google Calendar error: {exc}")
        sys.exit(1)


def cmd_listar(args):
    service = obtener_servicio()

    ahora = datetime.now(TZ_MADRID)
    hasta = ahora + timedelta(days=args.dias)

    eventos = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=ahora.isoformat(),
        timeMax=hasta.isoformat(),
        q="Solar meeting",
        singleEvents=True,
        orderBy="startTime",
        maxResults=50,
    ).execute().get("items", [])

    print(f"\n{'─'*60}")
    print(f"  SOLAR MEETINGS — next {args.dias} days ({len(eventos)} found)")
    print(f"{'─'*60}")

    if not eventos:
        print("  No solar meetings scheduled.")
        return

    for ev in eventos:
        start_raw = ev["start"].get("dateTime", ev["start"].get("date", ""))
        try:
            start = datetime.fromisoformat(start_raw).astimezone(TZ_MADRID)
            fecha_str = start.strftime("%a %d/%m %H:%M")
        except Exception:
            fecha_str = start_raw[:16]
        print(f"  {fecha_str}  {ev.get('summary', '')[:55]}")

    print(f"{'─'*60}")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Solar calendar management with Google Calendar"
    )
    subp = parser.add_subparsers(dest="cmd")

    # -- proponer
    p_prop = subp.add_parser("--proponer", add_help=False)
    p_prop.add_argument("--nombre", required=True)
    p_prop.add_argument("--dias",   type=int, default=7)

    # -- crear
    p_crear = subp.add_parser("--crear", add_help=False)
    p_crear.add_argument("--nombre", required=True)
    p_crear.add_argument("--inicio", required=True,
                         help="ISO datetime: 2024-01-15T10:00")

    # -- listar
    p_list = subp.add_parser("--listar", add_help=False)
    p_list.add_argument("--dias", type=int, default=14)

    # Manual parsing to support --proponer / --crear / --listar as flags
    raw = sys.argv[1:]
    if not raw:
        parser.print_help()
        return

    if "--proponer" in raw:
        idx = raw.index("--proponer")
        raw.pop(idx)
        ns = argparse.Namespace(nombre=None, dias=7)
        i = 0
        while i < len(raw):
            if raw[i] == "--nombre" and i+1 < len(raw):
                ns.nombre = raw[i+1]; i += 2
            elif raw[i] == "--dias" and i+1 < len(raw):
                ns.dias = int(raw[i+1]); i += 2
            else:
                i += 1
        if not ns.nombre:
            print("ERROR: --proponer requires --nombre")
            sys.exit(1)
        cmd_proponer(ns)

    elif "--crear" in raw:
        idx = raw.index("--crear")
        raw.pop(idx)
        ns = argparse.Namespace(nombre=None, inicio=None)
        i = 0
        while i < len(raw):
            if raw[i] == "--nombre" and i+1 < len(raw):
                ns.nombre = raw[i+1]; i += 2
            elif raw[i] == "--inicio" and i+1 < len(raw):
                ns.inicio = raw[i+1]; i += 2
            else:
                i += 1
        if not ns.nombre or not ns.inicio:
            print("ERROR: --crear requires --nombre and --inicio")
            sys.exit(1)
        cmd_crear(ns)

    elif "--listar" in raw:
        ns = argparse.Namespace(dias=14)
        i = 0
        while i < len(raw):
            if raw[i] == "--dias" and i+1 < len(raw):
                ns.dias = int(raw[i+1]); i += 2
            else:
                i += 1
        cmd_listar(ns)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
