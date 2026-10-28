"""
Aca esta la persistencia de datos

Cubre:
    - Registros de tamaño fijo (100 bytes) en contactos.dat.
    - Acceso aleatorio con seek() / tell(): se salta directo al registro.
    - Modificación en el lugar: solo se sobrescribe el registro editado.
    - Índice primario en JSON (indice.json): id -> número de registro.
    - Hash SHA-256 (contactos.sha256) y verificación de integridad.
    - Exportación a JSON y reporte PDF (fpdf2).
    - Respaldo ZIP (contactos.dat + indice.json).
    - Metadatos y permisos (os.stat / os.access).
    - Errores controlados con excepciones propias (nunca se cae la app).

Todo se lee y escribe en UTF-8, como dictamina la FIFA
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

ENCODING = "utf-8"


CAMPOS: tuple[tuple[str, int], ...] = (
    ("id", 6),
    ("nombre", 30),
    ("telefono", 15),
    ("correo", 30),
    ("ciudad", 18),
)
DATA_BYTES = sum(ancho for _, ancho in CAMPOS)  # 99
TERMINADOR = b"\n"  # facilita abrir el .dat en un editor de texto
RECORD_SIZE = DATA_BYTES + len(TERMINADOR)  # 100 bytes por registro
MAX_ID = 10 ** CAMPOS[0][1] - 1  # 999999

INDEX_VERSION = 1
DAT_NAME = "contactos.dat"
INDEX_NAME = "indice.json"
HASH_NAME = "contactos.sha256"

_RE_CORREO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RE_TELEFONO = re.compile(r"^[0-9+\-()\s]+$")
_RE_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

logger = logging.getLogger("contact_store")
logger.setLevel(logging.DEBUG)


def configure_logging(data_dir: Path) -> None:
    """Registra los eventos de la app en <data_dir>/app.log (una sola vez)."""
    log_path = Path(data_dir) / "app.log"
    for h in logger.handlers:
        if getattr(h, "baseFilename", None) == str(log_path.resolve()):
            return
    try:
        handler = logging.FileHandler(log_path, encoding=ENCODING)
    except OSError:
        return  # sin log no se cae la app
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)


# ---------------------------------------------------------------------------
# Excepciones propias
# ---------------------------------------------------------------------------
class StoreError(Exception):
    """Base de todos los errores controlados de la agenda."""


class ContactValidationError(StoreError):
    """Datos del contacto inválidos (vacíos, muy largos, formato incorrecto)."""


class DuplicateIdError(StoreError):
    """Ya existe un contacto con ese id."""


class ContactNotFoundError(StoreError):
    """No existe un contacto con ese id."""


class DataFileCorruptError(StoreError):
    """contactos.dat está corrupto (tamaño o contenido inválido)."""


class StorePermissionError(StoreError):
    """No hay permisos de lectura/escritura sobre un archivo o directorio."""


@contextmanager
def _guard(path: Path, accion: str) -> Iterator[None]:
    """Traduce errores del sistema operativo a excepciones propias."""
    try:
        yield
    except PermissionError as exc:
        logger.error("Sin permisos para %s %s: %s", accion, path, exc)
        raise StorePermissionError(
            f"Sin permisos para {accion} «{path.name}»."
        ) from exc
    except OSError as exc:
        logger.error("Error de E/S al %s %s: %s", accion, path, exc)
        raise StoreError(
            f"No se pudo {accion} «{path.name}»: {exc.strerror or exc}"
        ) from exc


def _write_text_atomic(path: Path, text: str) -> None:
    """Escribe a un .tmp y reemplaza el destino con os.replace (atómico)."""
    tmp = path.with_name(path.name + ".tmp")
    with _guard(path, "escribir"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding=ENCODING, newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


# ----
