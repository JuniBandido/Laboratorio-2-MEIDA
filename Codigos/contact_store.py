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


# ---------------------------------------------------------------------------
def _nbytes(texto: str) -> int:
    return len(texto.encode(ENCODING))


def _rellenar(valor: str, ancho: int) -> bytes:
    """Rellena con espacios (ljust) hasta `ancho` BYTES."""
    ancho_en_chars = ancho - (_nbytes(valor) - len(valor))
    return valor.ljust(ancho_en_chars).encode(ENCODING)


@dataclass
class Contact:
    id: int
    nombre: str
    telefono: str = ""
    correo: str = ""
    ciudad: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def normalizado(self) -> "Contact":
        return Contact(
            id=self.id,
            nombre=str(self.nombre).strip(),
            telefono=str(self.telefono).strip(),
            correo=str(self.correo).strip(),
            ciudad=str(self.ciudad).strip(),
        )

    def validar(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, int):
            raise ContactValidationError("El ID debe ser un número entero.")
        if not 1 <= self.id <= MAX_ID:
            raise ContactValidationError(f"El ID debe estar entre 1 y {MAX_ID}.")
        if not self.nombre:
            raise ContactValidationError("El nombre es obligatorio.")
        for nombre_campo, ancho in CAMPOS[1:]:
            valor = getattr(self, nombre_campo)
            if _RE_CONTROL.search(valor):
                raise ContactValidationError(
                    f"El campo «{nombre_campo}» contiene caracteres de control no permitidos."
                )
            if _nbytes(valor) > ancho:
                raise ContactValidationError(
                    f"El campo «{nombre_campo}» excede el máximo de {ancho} bytes "
                    f"({_nbytes(valor)} bytes)."
                )
        if self.telefono and not _RE_TELEFONO.match(self.telefono):
            raise ContactValidationError(
                "El teléfono solo puede contener dígitos, espacios, +, - y paréntesis."
            )
        if self.correo and not _RE_CORREO.match(self.correo):
            raise ContactValidationError("El correo no tiene un formato válido.")


def encode_record(contacto: Contact) -> bytes:
    """Contacto -> exactamente RECORD_SIZE bytes."""
    partes = [_rellenar(str(contacto.id), CAMPOS[0][1])]
    for nombre_campo, ancho in CAMPOS[1:]:
        partes.append(_rellenar(getattr(contacto, nombre_campo), ancho))
    raw = b"".join(partes) + TERMINADOR
    assert len(raw) == RECORD_SIZE, "el registro no mide RECORD_SIZE bytes"
    return raw


def decode_record(raw: bytes, numero: int = -1) -> Contact:
    """RECORD_SIZE bytes -> Contact. Lanza DataFileCorruptError si no es válido."""
    etiqueta = f"Registro #{numero}" if numero >= 0 else "Registro"
    if len(raw) != RECORD_SIZE or raw[-1:] != TERMINADOR:
        raise DataFileCorruptError(
            f"{etiqueta} corrupto: no mide {RECORD_SIZE} bytes o le falta el terminador."
        )
    valores: list[str] = []
    pos = 0
    try:
        for _, ancho in CAMPOS:
            valores.append(raw[pos : pos + ancho].decode(ENCODING).strip())
            pos += ancho
    except UnicodeDecodeError as exc:
        raise DataFileCorruptError(f"{etiqueta} corrupto: UTF-8 inválido.") from exc
    try:
        cid = int(valores[0])
    except ValueError as exc:
        raise DataFileCorruptError(f"{etiqueta} corrupto: el ID no es numérico.") from exc
    return Contact(cid, *valores[1:])


# ---------------------------------------------------------------------------
# Resultados
# ---------------------------------------------------------------------------
@dataclass
class AccessTrace:
    """Evidencia del acceso aleatorio: dónde se hizo seek() y qué dijo tell()."""

    operacion: str
    registro: int
    offset: int
    tell_antes: int = -1
    tell_despues: int = -1
    bytes_io: int = 0

    def describir(self) -> str:
        return (
            f"{self.operacion}: registro #{self.registro} → seek({self.registro} × "
            f"{RECORD_SIZE} = {self.offset}) · tell()={self.tell_antes} · "
            f"{self.bytes_io} bytes · tell() final={self.tell_despues}"
        )


class IntegrityStatus:
    OK = "ok"
    MODIFICADO = "modificado"
    SIN_HASH = "sin_hash"
    HASH_INVALIDO = "hash_invalido"
    SIN_ARCHIVO = "sin_archivo"


@dataclass
class IntegrityReport:
    estado: str
    hash_guardado: Optional[str]
    hash_actual: Optional[str]
    mensaje: str

    @property
    def ok(self) -> bool:
        return self.estado == IntegrityStatus.OK


@dataclass
class StartupReport:
    integridad: IntegrityReport
    avisos: list[str]


@dataclass
class BackupResult:
    ruta: Path
    tamano_original: int
    tamano_comprimido: int  # suma de los datos comprimidos dentro del ZIP
    tamano_zip: int  # tamaño real del archivo .zip (incluye cabeceras)
    archivos: list[str]

    @property
    def ahorro_pct(self) -> float:
        if not self.tamano_original:
            return 0.0
        return (1 - self.tamano_zip / self.tamano_original) * 100


@dataclass
class FileInfo:
    nombre: str
    ruta: Path
    existe: bool
    tamano: int = 0
    modificado: Optional[datetime] = None
    lectura: bool = False
    escritura: bool = False


def compute_hash(path: Path) -> str:
    """SHA-256 del archivo, leído por bloques."""
    h = hashlib.sha256()
    with _guard(path, "leer"):
        with open(path, "rb") as f:
            for bloque in iter(lambda: f.read(65536), b""):
                h.update(bloque)
    return h.hexdigest()


class _IndexProblem(Exception):
    """Interno: el índice no existe, no se puede leer o es inválido."""

