"""
Interfaz de todo el sistema owo
"""

from __future__ import annotations

import argparse
import functools
import sys
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import ttkbootstrap as tb
from ttkbootstrap.constants import *  # noqa: F401,F403

from contact_store import (
    CAMPOS, RECORD_SIZE, Contact, ContactStore, ContactNotFoundError,
    ContactValidationError, IntegrityStatus, StoreError, logger,
)

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def formato_bytes(n: int) -> str:
    return f"{n:,} bytes".replace(",", " ")


def manejar_errores(titulo: str):
    """Decorador: ningún error controlado (ni inesperado) tira la aplicación, espero"""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            padre = self.winfo_toplevel()
            try:
                return fn(self, *args, **kwargs)
            except ContactNotFoundError as exc:
                messagebox.showwarning("ID no encontrado", str(exc), parent=padre)
            except StoreError as exc:
                messagebox.showerror(titulo, str(exc), parent=padre)
            except Exception as exc:  # último recurso: registrar y avisar
                logger.exception("Error inesperado en %s", fn.__name__)
                messagebox.showerror(
                    titulo, f"Ocurrió un error inesperado:\n{exc}", parent=padre
                )
            return None

        return wrapper

    return deco
