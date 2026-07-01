"""
Local HTTP server so n8n can run the Python scripts from Docker.

Listens on http://0.0.0.0:8765 and exposes endpoints that n8n calls
via http://host.docker.internal:8765 (the Windows host from containers).

Endpoints:
  POST /pipeline   → runs pipeline.py --force --silencioso
  POST /emails     → runs email_sender.py --force
  POST /followup   → runs followup.py --enviar --dias 3 --force
  GET  /estado     → status of all JSON data files
  GET  /health     → uptime, active scripts, memory usage

Usage:
  python api_server.py           # default port 8765
  python api_server.py --port 8765

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WINDOWS TASK SCHEDULER (auto-start on login)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Open PowerShell as Administrator and run:

  $python  = (Get-Command pythonw.exe).Source
  $script  = "C:\\Users\\Porras\\proyectosolar\\agente-solar\\src\\api_server.py"
  $workdir = "C:\\Users\\Porras\\proyectosolar\\agente-solar\\src"

  $accion  = New-ScheduledTaskAction -Execute $python `
               -Argument $script -WorkingDirectory $workdir
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  $ajustes = New-ScheduledTaskSettingsSet `
               -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
               -RestartCount 3 `
               -RestartInterval (New-TimeSpan -Minutes 2) `
               -StartWhenAvailable $true
  Register-ScheduledTask -TaskName "Solexia Server" `
    -Action $accion -Trigger $trigger -Settings $ajustes `
    -RunLevel Highest -Force

Management commands:
  Check:   Get-ScheduledTask   -TaskName "Solexia Server"
  Start:   Start-ScheduledTask -TaskName "Solexia Server"
  Stop:    Stop-ScheduledTask  -TaskName "Solexia Server"
  Remove:  Unregister-ScheduledTask -TaskName "Solexia Server" -Confirm:$false

Notes:
  - Uses pythonw.exe (not python.exe) to avoid opening a console window.
  - ExecutionTimeLimit=0 removes the time limit (server runs indefinitely).
  - RestartCount=3 restarts the process if it crashes, with 2-minute intervals.
  - The AtLogOn trigger starts the server on Windows login.
  - Activity log is stored in logs/server.log.
"""

import sys
import os
import json
import argparse
import subprocess
import threading
import logging
import secrets
from http.server import HTTPServer, BaseHTTPRequestHandler
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent         # = src/
DATA_DIR = BASE_DIR.parent / "data"      # = project root/data/
LOG_FILE = BASE_DIR.parent / "logs" / "server.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# ─── Rotating log ────────────────────────────────────────────────────────────
# File: detailed format with module and line for diagnosis
# Console: short format for real-time monitoring

FMT_ARCHIVO = "%(asctime)s %(levelname)-8s [%(module)s:%(lineno)d] %(message)s"
FMT_CONSOLA = "%(asctime)s %(levelname)-8s %(message)s"
FMT_FECHA   = "%Y-%m-%d %H:%M:%S"

_handler_archivo = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_handler_archivo.setFormatter(logging.Formatter(FMT_ARCHIVO, datefmt=FMT_FECHA))
_handler_archivo.setLevel(logging.DEBUG)

_handler_consola = logging.StreamHandler(sys.stdout)
_handler_consola.setFormatter(logging.Formatter(FMT_CONSOLA, datefmt="%H:%M:%S"))
_handler_consola.setLevel(logging.INFO)

logging.root.setLevel(logging.DEBUG)
logging.root.addHandler(_handler_archivo)
logging.root.addHandler(_handler_consola)

log = logging.getLogger(__name__)

# Capture unhandled exceptions in any thread
def _excepthook_hilo(args):
    log.error(
        "Unhandled exception in thread '%s': %s",
        args.thread.name if args.thread else "unknown",
        args.exc_value,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )

threading.excepthook = _excepthook_hilo

# Capture unhandled exceptions in the main thread
def _excepthook_global(tipo, valor, tb):
    log.critical("Unhandled exception in main thread", exc_info=(tipo, valor, tb))

sys.excepthook = _excepthook_global

# Start time for uptime calculation in /health
_T_INICIO = datetime.now(timezone.utc)

# ─── Script definitions ───────────────────────────────────────────────────────

SCRIPTS: dict[str, dict] = {
    "pipeline": {
        "cmd":         [sys.executable, "pipeline.py", "--force", "--silencioso"],
        "descripcion": "Full pipeline (scraper → scoring → emails → pvgis → pdfs)",
        "timeout":     7200,
    },
    "emails": {
        "cmd":         [sys.executable, "email_sender.py", "--force"],
        "descripcion": "Send pending emails via Brevo SMTP",
        "timeout":     3600,
    },
    "followup": {
        "cmd":         [sys.executable, "followup.py", "--enviar", "--dias", "3", "--force"],
        "descripcion": "Follow up on leads without response (≥3 days)",
        "timeout":     3600,
    },
}

_API_KEY = os.getenv("SERVIDOR_API_KEY", "")
_lock    = threading.Lock()
_running: dict[str, bool] = {}


# ─── Server with global error handling ───────────────────────────────────────

class ServidorRobusto(HTTPServer):
    """HTTPServer that logs connection errors without stopping the process."""

    def handle_error(self, request, client_address):
        log.error(
            "Unexpected error serving request from %s:%s",
            *client_address,
            exc_info=True,
        )
        # Do not re-raise the exception: server keeps listening


# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class ScriptHandler(BaseHTTPRequestHandler):

    def _verificar_api_key(self) -> bool:
        if not _API_KEY:
            return True
        clave = self.headers.get("X-API-Key", "")
        return secrets.compare_digest(clave, _API_KEY)

    def _responder(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        origin = self.headers.get("Origin", "")
        if origin in ("http://host.docker.internal", "http://localhost"):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        if origin in ("http://host.docker.internal", "http://localhost"):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-API-Key, Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            self._do_GET_interno()
        except Exception:
            log.error("Unexpected error in do_GET %s", self.path, exc_info=True)
            try:
                self._responder(500, {"error": "Internal server error. See server.log."})
            except Exception:
                pass

    def _do_GET_interno(self):
        if not self._verificar_api_key():
            self._responder(401, {"error": "Unauthorised. Include the X-API-Key header."})
            return

        if self.path == "/health":
            ahora   = datetime.now(timezone.utc)
            uptime  = ahora - _T_INICIO
            horas   = int(uptime.total_seconds() // 3600)
            minutos = int((uptime.total_seconds() % 3600) // 60)

            # Process memory usage (no external dependencies)
            mem_mb: float | None = None
            try:
                import psutil
                mem_mb = round(psutil.Process().memory_info().rss / 1024 / 1024, 1)
            except ImportError:
                pass

            scripts_activos = [k for k, v in _running.items() if v]
            log_size_kb = round(LOG_FILE.stat().st_size / 1024, 1) if LOG_FILE.exists() else 0

            payload: dict = {
                "status":             "ok",
                "uptime":             f"{horas}h {minutos:02d}m",
                "inicio":             _T_INICIO.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "scripts_activos":    scripts_activos,
                "scripts_disponibles": list(SCRIPTS.keys()),
                "log_kb":             log_size_kb,
            }
            if mem_mb is not None:
                payload["memoria_mb"] = mem_mb

            log.debug("GET /health → ok (uptime %sh %sm)", horas, minutos)
            self._responder(200, payload)

        elif self.path == "/estado":
            archivos = {}
            for nombre in ["companies.json", "qualified_leads.json",
                           "generated_emails.json", "leads_with_solar_data.json"]:
                ruta = DATA_DIR / nombre
                if ruta.exists():
                    try:
                        datos = json.loads(ruta.read_text(encoding="utf-8"))
                        archivos[nombre] = {
                            "existe":     True,
                            "registros":  len(datos) if isinstance(datos, list) else 1,
                            "modificado": datetime.fromtimestamp(
                                ruta.stat().st_mtime
                            ).strftime("%d/%m/%Y %H:%M"),
                        }
                    except Exception:
                        archivos[nombre] = {"existe": True, "registros": "?"}
                else:
                    archivos[nombre] = {"existe": False}

            proposals = DATA_DIR / "proposals"
            archivos["proposals/"] = {
                "existe":    proposals.exists(),
                "registros": len(list(proposals.glob("*.pdf"))) if proposals.exists() else 0,
            }
            self._responder(200, {"estado": archivos})

        else:
            log.warning("GET %s → 404", self.path)
            self._responder(404, {"error": f"Route not found: {self.path}"})

    def do_POST(self):
        try:
            self._do_POST_interno()
        except Exception:
            log.error("Unexpected error in do_POST %s", self.path, exc_info=True)
            try:
                self._responder(500, {"error": "Internal server error. See server.log."})
            except Exception:
                pass

    def _do_POST_interno(self):
        if not self._verificar_api_key():
            self._responder(401, {"error": "Unauthorised. Include the X-API-Key header."})
            return

        nombre = self.path.lstrip("/")

        if nombre not in SCRIPTS:
            log.warning("POST /%s → unrecognised script", nombre)
            self._responder(404, {
                "error": f"Script '{nombre}' not recognised",
                "disponibles": list(SCRIPTS.keys()),
            })
            return

        if _running.get(nombre):
            log.warning("POST /%s → already running, rejected", nombre)
            self._responder(409, {
                "error": f"'{nombre}' is already running. Wait for it to finish.",
            })
            return

        script = SCRIPTS[nombre]
        log.info("▶ Starting: %s — %s", nombre, script["descripcion"])

        acquired = _lock.acquire(blocking=False)
        if not acquired:
            log.warning("POST /%s → lock busy, rejected", nombre)
            self._responder(503, {
                "error": "Another script is already running. Try again shortly.",
            })
            return

        _running[nombre] = True
        t_inicio = datetime.now()

        try:
            resultado = subprocess.run(
                script["cmd"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(BASE_DIR),
                timeout=script["timeout"],
            )
            t_total = (datetime.now() - t_inicio).seconds
            ok = resultado.returncode == 0

            if ok:
                log.info("✓ %s completed in %ds (exit 0)", nombre, t_total)
            else:
                log.error(
                    "✗ %s ended with error in %ds (exit %d)\nSTDERR: %s",
                    nombre, t_total, resultado.returncode,
                    resultado.stderr[-500:] if resultado.stderr else "(empty)",
                )

            self._responder(200 if ok else 500, {
                "ok":         ok,
                "script":     nombre,
                "exit_code":  resultado.returncode,
                "stdout":     resultado.stdout[-2000:] if resultado.stdout else "",
                "stderr":     resultado.stderr[-500:]  if resultado.stderr else "",
                "duracion_s": t_total,
                "timestamp":  t_inicio.isoformat(),
            })

        except subprocess.TimeoutExpired:
            t_total = (datetime.now() - t_inicio).seconds
            log.error("✗ %s exceeded timeout of %ds after %ds", nombre, script["timeout"], t_total)
            self._responder(504, {
                "ok": False, "error": "timeout",
                "script": nombre, "timeout_s": script["timeout"],
            })

        except Exception as exc:
            log.error("✗ Unexpected error running %s: %s", nombre, exc, exc_info=True)
            self._responder(500, {"ok": False, "error": str(exc), "script": nombre})

        finally:
            _running[nombre] = False
            _lock.release()

    def log_message(self, fmt, *args):
        # Silence BaseHTTPRequestHandler's default HTTP log;
        # we use our own logging in each method
        pass


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    server = ServidorRobusto((args.host, args.port), ScriptHandler)

    log.info("=" * 56)
    log.info("  Solexia script server starting")
    log.info("  Listening on:  http://%s:%d", args.host, args.port)
    log.info("  n8n calls:     http://host.docker.internal:%d", args.port)
    log.info("  Available scripts:")
    for nombre, cfg in SCRIPTS.items():
        log.info("    POST /%-14s → %s", nombre, cfg["descripcion"])
    log.info("    GET  /health         → uptime and status")
    log.info("    GET  /estado         → JSON files")
    log.info("  Rotating log: %s (max 5 MB × 5 files)", LOG_FILE.name)
    log.info("=" * 56)

    print("  Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server stopped by user (Ctrl+C)")
        server.server_close()


if __name__ == "__main__":
    main()
