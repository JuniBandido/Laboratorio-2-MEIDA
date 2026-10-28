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


# ---------------------------------------------------------------------------
# Almacén principal
# ---------------------------------------------------------------------------
class ContactStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.dat_path = self.data_dir / DAT_NAME
        self.index_path = self.data_dir / INDEX_NAME
        self.hash_path = self.data_dir / HASH_NAME
        self.exports_dir = self.data_dir / "exports"
        self.backups_dir = self.data_dir / "backups"
        self._index: dict[int, int] = {}

    # -- arranque -----------------------------------------------------------
    def initialize(self) -> StartupReport:
        """Prepara carpeta, verifica el hash y carga (o reconstruye) el índice.
        Nunca lanza por archivos ausentes/corruptos: devuelve avisos."""
        avisos: list[str] = []
        with _guard(self.data_dir, "crear la carpeta de datos"):
            self.data_dir.mkdir(parents=True, exist_ok=True)
        configure_logging(self.data_dir)
        logger.info("Inicio de la aplicación. Datos en %s", self.data_dir)

        try:
            integridad = self.verify_integrity()
        except StoreError as exc:
            integridad = IntegrityReport(
                IntegrityStatus.HASH_INVALIDO, None, None, str(exc)
            )
        if integridad.estado == IntegrityStatus.SIN_HASH:
            try:
                self.seal()
                avisos.append("No existía contactos.sha256; se generó con el estado actual.")
            except StoreError as exc:
                avisos.append(str(exc))
        elif not integridad.ok and integridad.estado != IntegrityStatus.SIN_ARCHIVO:
            avisos.append(integridad.mensaje)

        avisos.extend(self._load_index())
        return StartupReport(integridad, avisos)

    # -- conteos ------------------------------------------------------------
    def _record_count(self) -> int:
        with _guard(self.dat_path, "leer"):
            try:
                size = self.dat_path.stat().st_size
            except FileNotFoundError:
                return 0
        if size % RECORD_SIZE:
            raise DataFileCorruptError(
                f"«{self.dat_path.name}» está corrupto: su tamaño ({size} bytes) no es "
                f"múltiplo del tamaño de registro ({RECORD_SIZE} bytes)."
            )
        return size // RECORD_SIZE

    def count(self) -> int:
        return len(self._index)

    def next_id(self) -> int:
        return (max(self._index) + 1) if self._index else 1

    def record_number(self, contact_id: int) -> Optional[int]:
        return self._index.get(contact_id)

    # -- índice -------------------------------------------------------------
    def _read_index_file(self) -> dict[int, int]:
        try:
            with _guard(self.index_path, "leer"):
                texto = self.index_path.read_text(encoding=ENCODING)
        except StorePermissionError as exc:
            raise _IndexProblem("no se puede leer (sin permisos)") from exc
        except StoreError as exc:
            if not self.index_path.exists():
                raise _IndexProblem("no existe") from exc
            raise _IndexProblem("no se puede leer") from exc
        except UnicodeDecodeError as exc:
            raise _IndexProblem("está corrupto") from exc
        try:
            datos = json.loads(texto)
            if datos.get("version") != INDEX_VERSION:
                raise ValueError("versión desconocida")
            crudo = datos["indice"]
            return {int(k): int(v) for k, v in crudo.items()}
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError) as exc:
            raise _IndexProblem("está corrupto") from exc

    def _save_index(self) -> None:
        payload = {
            "version": INDEX_VERSION,
            "tamano_registro": RECORD_SIZE,
            "total": len(self._index),
            "indice": {str(k): v for k, v in sorted(self._index.items())},
        }
        _write_text_atomic(
            self.index_path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        )

    def _load_index(self) -> list[str]:
        try:
            n = self._record_count()
        except StoreError as exc:
            self._index = {}
            logger.error("Archivo de datos inválido al iniciar: %s", exc)
            return [str(exc)]
        motivo = ""
        try:
            idx = self._read_index_file()
            if len(idx) == n and set(idx.values()) == set(range(n)):
                self._index = idx
                return []
            motivo = "no coincide con el archivo de contactos"
        except _IndexProblem as exc:
            motivo = str(exc)
            if motivo == "no existe" and n == 0:
                self._index = {}
                return []
        try:
            total = self.rebuild_index()
        except StoreError as exc:
            self._index = {}
            return [f"El índice {motivo} y no se pudo reconstruir: {exc}"]
        logger.warning("Índice %s; reconstruido (%d registros)", motivo, total)
        return [f"El índice {motivo}; se reconstruyó leyendo contactos.dat ({total} registros)."]

    def rebuild_index(self) -> int:
        """Reconstruye el índice recorriendo contactos.dat registro por registro."""
        n = self._record_count()
        nuevo: dict[int, int] = {}
        if n:
            with _guard(self.dat_path, "leer"):
                with open(self.dat_path, "rb") as f:
                    for pos in range(n):
                        f.seek(pos * RECORD_SIZE)
                        c = decode_record(f.read(RECORD_SIZE), pos)
                        if c.id in nuevo:
                            raise DataFileCorruptError(
                                f"ID {c.id} duplicado en los registros #{nuevo[c.id]} y #{pos}."
                            )
                        nuevo[c.id] = pos
        self._index = nuevo
        if n or self.index_path.exists():
            self._save_index()
        return len(nuevo)

    # -- lectura / escritura de registros -----------------------------------
    def _read_record(self, pos: int) -> tuple[Contact, AccessTrace]:
        traza = AccessTrace("LEER", pos, pos * RECORD_SIZE)
        with _guard(self.dat_path, "leer"):
            with open(self.dat_path, "rb") as f:
                f.seek(traza.offset)
                traza.tell_antes = f.tell()
                raw = f.read(RECORD_SIZE)
                traza.tell_despues = f.tell()
        traza.bytes_io = len(raw)
        return decode_record(raw, pos), traza

    def get(self, contact_id: int) -> tuple[Contact, AccessTrace]:
        """Búsqueda por id: primero el índice, luego seek() al registro."""
        for intento in range(2):
            pos = self._index.get(contact_id)
            if pos is None or not self.dat_path.exists():
                break
            contacto, traza = self._read_record(pos)
            if contacto.id == contact_id:
                return contacto, traza
            logger.warning("Índice desactualizado (id %s); se reconstruye", contact_id)
            self.rebuild_index()
        raise ContactNotFoundError(f"No existe ningún contacto con ID {contact_id}.")

    def add(self, contacto: Contact) -> AccessTrace:
        c = contacto.normalizado()
        c.validar()
        if c.id in self._index:
            raise DuplicateIdError(f"Ya existe un contacto con ID {c.id}.")
        n = self._record_count()
        traza = AccessTrace("AGREGAR", n, n * RECORD_SIZE)
        raw = encode_record(c)
        with _guard(self.dat_path, "escribir en"):
            self.data_dir.mkdir(parents=True, exist_ok=True)
            modo = "r+b" if self.dat_path.exists() else "w+b"
            with open(self.dat_path, modo) as f:
                f.seek(traza.offset)
                traza.tell_antes = f.tell()
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
                traza.tell_despues = f.tell()
        traza.bytes_io = len(raw)
        self.seal()
        self._index[c.id] = n
        self._save_index()
        logger.info("Contacto agregado: id=%s registro=%s", c.id, n)
        return traza

    def update(self, contacto: Contact) -> AccessTrace:
        """Sobrescribe SOLO el registro del contacto; el resto no se reescribe."""
        c = contacto.normalizado()
        c.validar()
        pos = self._index.get(c.id)
        if pos is None or not self.dat_path.exists():
            raise ContactNotFoundError(f"No existe ningún contacto con ID {c.id}.")
        traza = AccessTrace("MODIFICAR", pos, pos * RECORD_SIZE)
        raw = encode_record(c)
        with _guard(self.dat_path, "escribir en"):
            with open(self.dat_path, "r+b") as f:
                f.seek(traza.offset)
                actual = decode_record(f.read(RECORD_SIZE), pos)
                if actual.id != c.id:
                    raise DataFileCorruptError(
                        "El índice no coincide con el archivo; use Herramientas > "
                        "Verificar integridad para reconstruirlo."
                    )
                f.seek(traza.offset)
                traza.tell_antes = f.tell()
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
                traza.tell_despues = f.tell()
        traza.bytes_io = len(raw)
        self.seal()
        logger.info("Contacto modificado: id=%s registro=%s", c.id, pos)
        return traza

    def list_all(self) -> list[Contact]:
        """Lectura secuencial de todos los registros (para tabla y exportación)."""
        n = self._record_count()
        contactos: list[Contact] = []
        if not n:
            return contactos
        with _guard(self.dat_path, "leer"):
            with open(self.dat_path, "rb") as f:
                for pos in range(n):
                    contactos.append(decode_record(f.read(RECORD_SIZE), pos))
        return contactos

    # -- integridad ---------------------------------------------------------
    def seal(self) -> str:
        """Calcula el SHA-256 de contactos.dat y lo guarda en contactos.sha256."""
        if not self.dat_path.exists():
            return ""
        digest = compute_hash(self.dat_path)
        _write_text_atomic(self.hash_path, f"{digest}  {self.dat_path.name}\n")
        return digest

    def verify_integrity(self) -> IntegrityReport:
        if not self.dat_path.exists():
            return IntegrityReport(
                IntegrityStatus.SIN_ARCHIVO, None, None,
                "Aún no existe contactos.dat (no hay nada que verificar).",
            )
        actual = compute_hash(self.dat_path)
        try:
            with _guard(self.hash_path, "leer"):
                texto = self.hash_path.read_text(encoding=ENCODING)
        except StorePermissionError:
            raise
        except StoreError:
            if not self.hash_path.exists():
                return IntegrityReport(
                    IntegrityStatus.SIN_HASH, None, actual,
                    "No existe contactos.sha256; no se puede comparar.",
                )
            raise
        except UnicodeDecodeError:
            texto = ""
        partes = texto.split()
        guardado = partes[0].lower() if partes else ""
        if not re.fullmatch(r"[0-9a-f]{64}", guardado):
            return IntegrityReport(
                IntegrityStatus.HASH_INVALIDO, None, actual,
                "contactos.sha256 está dañado o no contiene un SHA-256 válido.",
            )
        if guardado == actual:
            return IntegrityReport(
                IntegrityStatus.OK, guardado, actual,
                "Integridad correcta: el hash SHA-256 coincide.",
            )
        logger.warning("Hash no coincide: guardado=%s actual=%s", guardado, actual)
        return IntegrityReport(
            IntegrityStatus.MODIFICADO, guardado, actual,
            "⚠ contactos.dat fue modificado fuera de la aplicación: "
            "el hash SHA-256 no coincide con el guardado.",
        )

    def accept_current_state(self) -> int:
        """Acepta el archivo tal como está: reconstruye el índice y renueva el hash."""
        total = self.rebuild_index()
        self.seal()
        logger.warning("Estado actual aceptado por el usuario (%d registros)", total)
        return total

    # -- respaldo -----------------------------------------------------------
    def create_backup(self) -> BackupResult:
        if not self.dat_path.exists():
            raise StoreError("No hay contactos.dat para respaldar todavía.")
        with _guard(self.backups_dir, "crear la carpeta de"):
            self.backups_dir.mkdir(parents=True, exist_ok=True)
        base = f"respaldo_{datetime.now():%Y%m%d_%H%M%S}"
        destino = self.backups_dir / f"{base}.zip"
        k = 1
        while destino.exists():
            destino = self.backups_dir / f"{base}_{k}.zip"
            k += 1
        fuentes = [self.dat_path] + ([self.index_path] if self.index_path.exists() else [])
        original = 0
        comprimido = 0
        with _guard(destino, "crear"):
            with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
                for fuente in fuentes:
                    z.write(fuente, arcname=fuente.name)
                for info in z.infolist():
                    original += info.file_size
                    comprimido += info.compress_size
            zip_size = destino.stat().st_size
        logger.info("Respaldo creado: %s (%d -> %d bytes)", destino, original, zip_size)
        return BackupResult(destino, original, comprimido, zip_size, [f.name for f in fuentes])

    # -- metadatos ----------------------------------------------------------
    def file_info(self) -> list[FileInfo]:
        resultado = []
        for ruta in (self.dat_path, self.index_path, self.hash_path):
            try:
                st = os.stat(ruta)
            except OSError:
                resultado.append(FileInfo(ruta.name, ruta, False))
                continue
            resultado.append(
                FileInfo(
                    ruta.name, ruta, True, st.st_size,
                    datetime.fromtimestamp(st.st_mtime),
                    os.access(ruta, os.R_OK), os.access(ruta, os.W_OK),
                )
            )
        return resultado

    # -- exportación --------------------------------------------------------
    def export_json(self, destino: Optional[Path] = None) -> tuple[Path, int]:
        destino = Path(destino) if destino else self.exports_dir / "contactos_export.json"
        contactos = self.list_all()
        payload = {
            "generado": datetime.now().astimezone().isoformat(timespec="seconds"),
            "archivo_origen": self.dat_path.name,
            "total": len(contactos),
            "contactos": [c.to_dict() for c in contactos],
        }
        _write_text_atomic(destino, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        logger.info("JSON exportado: %s (%d contactos)", destino, len(contactos))
        return destino, len(contactos)

    def export_pdf_from_json(self, json_path: Path, pdf_path: Optional[Path] = None) -> Path:
        """Lee el JSON exportado y genera el reporte PDF a partir de él."""
        json_path = Path(json_path)
        pdf_path = Path(pdf_path) if pdf_path else self.exports_dir / "reporte_contactos.pdf"
        try:
            with _guard(json_path, "leer"):
                datos = json.loads(json_path.read_text(encoding=ENCODING))
            filas = datos["contactos"]
            if not isinstance(filas, list):
                raise TypeError
        except (ValueError, KeyError, TypeError) as exc:
            raise StoreError(f"El JSON «{json_path.name}» es inválido o está corrupto.") from exc
        _construir_pdf(datos, pdf_path)
        logger.info("PDF generado: %s", pdf_path)
        return pdf_path

    def generate_report(
        self, pdf_path: Optional[Path] = None, json_path: Optional[Path] = None
    ) -> tuple[Path, Path, int]:
        """Exporta el JSON y, a partir de él, genera el PDF."""
        json_final, total = self.export_json(json_path)
        pdf_final = self.export_pdf_from_json(json_final, pdf_path)
        return json_final, pdf_final, total


# ---------------------------------------------------------------------------
# Reporte PDF (fpdf2)
# ---------------------------------------------------------------------------
_FUENTES_TTF = (
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)
_ANCHOS_PDF = (14, 50, 30, 64, 32)  # suman 190 mm (A4 con márgenes de 10 mm)
_TITULOS_PDF = ("ID", "Nombre", "Teléfono", "Correo", "Ciudad")


def _construir_pdf(datos: dict, destino: Path) -> None:
    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise StoreError("Falta la librería fpdf2. Instale con: pip install fpdf2") from exc

    unicode_ok = False
    familia = "Helvetica"

    class Reporte(FPDF):
        def footer(self) -> None:
            self.set_y(-12)
            self.set_font(familia, "", 8)
            self.cell(0, 8, f"Página {self.page_no()}/{{nb}}", align="C")

    pdf = Reporte(format="A4")
    pdf.alias_nb_pages()
    for regular, negrita in _FUENTES_TTF:
        if Path(regular).exists() and Path(negrita).exists():
            try:
                pdf.add_font("Cuerpo", "", regular)
                pdf.add_font("Cuerpo", "B", negrita)
                familia, unicode_ok = "Cuerpo", True
                break
            except Exception:  # fuente ilegible: se prueba la siguiente
                continue

    def t(texto: object) -> str:
        s = str(texto)
        return s if unicode_ok else s.encode("latin-1", "replace").decode("latin-1")

    def ajustar(texto: str, ancho: float) -> str:
        texto = t(texto)
        while texto and pdf.get_string_width(texto) > ancho - 2:
            texto = texto[:-1]
        return texto

    def encabezado_tabla() -> None:
        pdf.set_font(familia, "B", 9)
        pdf.set_fill_color(220, 220, 220)
        for titulo, ancho in zip(_TITULOS_PDF, _ANCHOS_PDF):
            pdf.cell(ancho, 7, t(titulo), border=1, align="C", fill=True)
        pdf.ln(7)

    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    pdf.set_font(familia, "B", 16)
    pdf.cell(0, 10, t("Agenda de Contactos"), align="C")
    pdf.ln(10)
    pdf.set_font(familia, "", 10)
    try:
        fecha = datetime.fromisoformat(datos.get("generado", "")).strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        fecha = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    pdf.cell(0, 6, t(f"Fecha de generación: {fecha}"))
    pdf.ln(6)
    pdf.cell(0, 6, t(f"Total de contactos: {len(datos['contactos'])}"))
    pdf.ln(9)

    encabezado_tabla()
    pdf.set_font(familia, "", 9)
    for i, fila in enumerate(datos["contactos"]):
        if pdf.get_y() + 7 > pdf.page_break_trigger:
            pdf.add_page()
            encabezado_tabla()
            pdf.set_font(familia, "", 9)
        relleno = i % 2 == 1
        pdf.set_fill_color(245, 245, 245)
        celdas = (fila.get("id", ""), fila.get("nombre", ""), fila.get("telefono", ""),
                  fila.get("correo", ""), fila.get("ciudad", ""))
        for j, (valor, ancho) in enumerate(zip(celdas, _ANCHOS_PDF)):
            pdf.cell(ancho, 7, ajustar(str(valor), ancho), border=1,
                     align="C" if j == 0 else "L", fill=relleno)
        pdf.ln(7)

    tmp = destino.with_name(destino.name + ".tmp")

    
    with _guard(destino, "escribir"):
        destino.parent.mkdir(parents=True, exist_ok=True)
        pdf.output(str(tmp))
        os.replace(tmp, destino)
