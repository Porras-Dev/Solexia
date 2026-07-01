"""
Utilidades compartidas por todos los scripts del pipeline Agente Solar.
"""


def barra(actual: int, total: int, ancho: int = 30) -> str:
    """Devuelve una barra de progreso ASCII: [████░░░░░░] actual/total"""
    relleno = int(ancho * actual / total) if total else 0
    return f"[{'█' * relleno}{'░' * (ancho - relleno)}] {actual}/{total}"
