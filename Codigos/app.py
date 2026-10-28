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


# ---------------------------------------------------------------------------
# Ventana de contactos: agregar, buscar por ID y modificar
# ---------------------------------------------------------------------------
class ContactsWindow(tb.Toplevel):
    ETIQUETAS = {
        "id": "ID", "nombre": "Nombre", "telefono": "Teléfono",
        "correo": "Correo", "ciudad": "Ciudad",
    }

    def __init__(self, app: "MainApp"):
        super().__init__(master=app)
        self.app = app
        self.store = app.store
        self.title("Contactos")
        self.geometry("1020x640")
        self.minsize(900, 560)
        self.protocol("WM_DELETE_WINDOW", self._cerrar)

        self.vars = {nombre: tb.StringVar() for nombre, _ in CAMPOS}

        contenedor = tb.Frame(self, padding=14)
        contenedor.pack(fill=BOTH, expand=True)
        contenedor.columnconfigure(1, weight=1)
        contenedor.rowconfigure(0, weight=1)

        # --- formulario ---------------------------------------------------
        form = tb.Labelframe(contenedor, text="Datos del contacto", padding=14)
        form.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        self.entradas = {}
        for fila, (nombre, ancho) in enumerate(CAMPOS):
            tb.Label(form, text=self.ETIQUETAS[nombre]).grid(row=fila * 2, column=0, sticky="w")
            ent = tb.Entry(form, textvariable=self.vars[nombre], width=34)
            ent.grid(row=fila * 2 + 1, column=0, sticky="ew", pady=(0, 8))
            self.entradas[nombre] = ent
            tb.Label(form, text=f"máx. {ancho} bytes", bootstyle="secondary",
                     font=("", 8)).grid(row=fila * 2, column=1, sticky="e", padx=(8, 0))

        botones = tb.Frame(form)
        botones.grid(row=len(CAMPOS) * 2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        botones.columnconfigure((0, 1), weight=1)
        tb.Button(botones, text="➕ Agregar", bootstyle="success",
                  command=self.agregar).grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=3)
        tb.Button(botones, text="🔍 Buscar por ID", bootstyle="info",
                  command=self.buscar).grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=3)
        tb.Button(botones, text="✏ Modificar", bootstyle="warning",
                  command=self.modificar).grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=3)
        tb.Button(botones, text="Limpiar", bootstyle="secondary-outline",
                  command=self.limpiar).grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=3)

        # --- tabla --------------------------------------------------------
        panel = tb.Labelframe(contenedor, text="Contactos guardados (doble clic = buscar por ID)", padding=8)
        panel.grid(row=0, column=1, sticky="nsew")
        panel.rowconfigure(0, weight=1)
        panel.columnconfigure(0, weight=1)
        columnas = ("reg", "id", "nombre", "telefono", "correo", "ciudad")
        self.tabla = tb.Treeview(panel, columns=columnas, show="headings", bootstyle="primary")
        anchos = {"reg": 50, "id": 60, "nombre": 190, "telefono": 110, "correo": 200, "ciudad": 110}
        titulos = {"reg": "Reg.", "id": "ID", "nombre": "Nombre", "telefono": "Teléfono",
                   "correo": "Correo", "ciudad": "Ciudad"}
        for c in columnas:
            self.tabla.heading(c, text=titulos[c])
            self.tabla.column(c, width=anchos[c], anchor="center" if c in ("reg", "id") else "w")
        sb = tb.Scrollbar(panel, orient="vertical", command=self.tabla.yview)
        self.tabla.configure(yscrollcommand=sb.set)
        self.tabla.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.tabla.bind("<Double-1>", self._doble_clic)

        # --- bitácora de acceso aleatorio (seek / tell) -------------------
        log = tb.Labelframe(contenedor, text="Acceso aleatorio: seek() / tell()", padding=8)
        log.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        log.columnconfigure(0, weight=1)
        self.txt_log = tb.Text(log, height=6, wrap="word", state="disabled")
        self.txt_log.grid(row=0, column=0, sticky="ew")
        sb2 = tb.Scrollbar(log, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb2.set)
        sb2.grid(row=0, column=1, sticky="ns")

        self.limpiar()
        self.refrescar_tabla()
        self._log(f"Tamaño de registro: {RECORD_SIZE} bytes. Posición = n.º de registro × {RECORD_SIZE}.")

