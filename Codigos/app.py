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

    # -- utilidades ---------------------------------------------------------
    def _cerrar(self):
        self.app.contactos_win = None
        self.destroy()

    def enfocar(self, modo: str):
        """Llamado desde el menú: deja el cursor donde corresponde."""
        self.lift()
        self.focus_force()
        self.entradas["id" if modo == "buscar" else "nombre"].focus_set()

    def _log(self, texto: str):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"[{datetime.now():%H:%M:%S}] {texto}\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _leer_id(self) -> int:
        texto = self.vars["id"].get().strip()
        try:
            valor = int(texto)
        except ValueError:
            raise ContactValidationError("El ID debe ser un número entero positivo.") from None
        return valor

    def _contacto_del_formulario(self) -> Contact:
        return Contact(
            id=self._leer_id(),
            nombre=self.vars["nombre"].get(),
            telefono=self.vars["telefono"].get(),
            correo=self.vars["correo"].get(),
            ciudad=self.vars["ciudad"].get(),
        )

    def _llenar(self, c: Contact):
        for nombre, valor in c.to_dict().items():
            self.vars[nombre].set(str(valor))

    def refrescar_tabla(self):
        self.tabla.delete(*self.tabla.get_children())
        try:
            contactos = self.store.list_all()
        except StoreError as exc:
            self._log(f"No se pudo listar: {exc}")
            return
        for pos, c in enumerate(contactos):
            self.tabla.insert("", "end", values=(pos, c.id, c.nombre, c.telefono, c.correo, c.ciudad))

    def _tras_cambio(self):
        self.refrescar_tabla()
        self.app.refrescar_estado()

    # -- acciones -----------------------------------------------------------
    def limpiar(self):
        for v in self.vars.values():
            v.set("")
        self.vars["id"].set(str(self.store.next_id()))
        self.entradas["nombre"].focus_set()

    @manejar_errores("Error al agregar")
    def agregar(self):
        traza = self.store.add(self._contacto_del_formulario())
        self._log(traza.describir())
        self._tras_cambio()
        messagebox.showinfo("Contacto agregado",
                            f"Contacto guardado en el registro #{traza.registro}.", parent=self)
        self.limpiar()

    @manejar_errores("Error al buscar")
    def buscar(self):
        cid = self._leer_id()
        contacto, traza = self.store.get(cid)
        self._llenar(contacto)
        self._log(f"Índice: id {cid} → registro #{traza.registro}")
        self._log(traza.describir())

    @manejar_errores("Error al modificar")
    def modificar(self):
        traza = self.store.update(self._contacto_del_formulario())
        self._log(traza.describir())
        self._tras_cambio()
        messagebox.showinfo("Contacto modificado",
                            f"Se sobrescribió solo el registro #{traza.registro}.", parent=self)

    def _doble_clic(self, _evento):
        seleccion = self.tabla.selection()
        if not seleccion:
            return
        self.vars["id"].set(str(self.tabla.item(seleccion[0], "values")[1]))
        self.buscar()


# ---------------------------------------------------------------------------
# Ventana de información del archivo (os.stat / os.access)
# ---------------------------------------------------------------------------
class InfoWindow(tb.Toplevel):
    def __init__(self, app: "MainApp"):
        super().__init__(master=app)
        self.title("Información del archivo")
        self.geometry("860x330")
        self.transient(app)
        marco = tb.Frame(self, padding=16)
        marco.pack(fill=BOTH, expand=True)

        columnas = ("archivo", "tamano", "modificado", "lectura", "escritura")
        tabla = tb.Treeview(marco, columns=columnas, show="headings", height=3, bootstyle="info")
        for c, t, a in (("archivo", "Archivo", 150), ("tamano", "Tamaño", 130),
                        ("modificado", "Última modificación", 190),
                        ("lectura", "Lectura", 90), ("escritura", "Escritura", 90)):
            tabla.heading(c, text=t)
            tabla.column(c, width=a, anchor="w" if c == "archivo" else "center")
        tabla.pack(fill=X)

        for info in app.store.file_info():
            if info.existe:
                tabla.insert("", "end", values=(
                    info.nombre, formato_bytes(info.tamano),
                    info.modificado.strftime("%d/%m/%Y %H:%M:%S"),
                    "Sí" if info.lectura else "No", "Sí" if info.escritura else "No"))
            else:
                tabla.insert("", "end", values=(info.nombre, "—", "No existe", "—", "—"))

        dat = app.store.file_info()[0]
        if dat.existe and dat.tamano % RECORD_SIZE == 0:
            registros = f"{dat.tamano // RECORD_SIZE} registros × {RECORD_SIZE} bytes"
        elif dat.existe:
            registros = "tamaño inválido (archivo corrupto)"
        else:
            registros = "0 registros"
        tb.Label(marco, text=f"Ruta: {app.store.dat_path}", wraplength=800,
                 justify="left").pack(anchor="w", pady=(14, 2))
        tb.Label(marco, text=f"Contenido de contactos.dat: {registros}").pack(anchor="w")
        tb.Button(marco, text="Cerrar", command=self.destroy,
                  bootstyle="secondary").pack(anchor="e", pady=(14, 0))


# ---------------------------------------------------------------------------
# Ventana principal
# ---------------------------------------------------------------------------
class MainApp(tb.Window):
    def __init__(self, data_dir: Path):
        super().__init__(themename="flatly")
        self.title("Agenda de Contactos — Acceso Aleatorio")
        self.geometry("760x430")
        self.minsize(640, 380)
        self.store = ContactStore(data_dir)
        self.contactos_win: ContactsWindow | None = None

        self._build_menu()
        self._build_ui()

        avisos: list[str] = []
        try:
            reporte = self.store.initialize()
            avisos = reporte.avisos
        except StoreError as exc:
            avisos = [str(exc)]
        self.refrescar_estado()
        if avisos:
            self.after(150, lambda: messagebox.showwarning(
                "Aviso de carga", "\n\n".join(avisos), parent=self))

    # -- construcción -------------------------------------------------------
    def _build_menu(self):
        barra = tb.Menu(self)

        m = tb.Menu(barra, tearoff=False)
        m.add_command(label="Agregar contacto…", command=lambda: self.abrir_contactos("agregar"))
        m.add_command(label="Buscar por ID…", command=lambda: self.abrir_contactos("buscar"))
        m.add_command(label="Modificar contacto…", command=lambda: self.abrir_contactos("modificar"))
        m.add_separator()
        m.add_command(label="Salir", command=self.destroy)
        barra.add_cascade(label="Contactos", menu=m)

        m = tb.Menu(barra, tearoff=False)
        m.add_command(label="Exportar a JSON…", command=self.exportar_json)
        m.add_command(label="Generar reporte PDF…", command=self.generar_pdf)
        barra.add_cascade(label="Exportar", menu=m)

        m = tb.Menu(barra, tearoff=False)
        m.add_command(label="Verificar integridad", command=self.verificar_integridad)
        m.add_command(label="Crear respaldo ZIP", command=self.crear_respaldo)
        m.add_command(label="Información del archivo", command=self.mostrar_info)
        barra.add_cascade(label="Herramientas", menu=m)

        m = tb.Menu(barra, tearoff=False)
        m.add_command(label="Manual de usuario", command=self._ayuda_simulada)
        m.add_command(label="Acerca de", command=self._ayuda_simulada)
        barra.add_cascade(label="Ayuda", menu=m)

        self.config(menu=barra)

    def _build_ui(self):
        marco = tb.Frame(self, padding=28)
        marco.pack(fill=BOTH, expand=True)
        tb.Label(marco, text="📇  Agenda de Contactos", font=("", 22, "bold")).pack(anchor="w")
        tb.Label(marco, text="Registros de tamaño fijo · acceso aleatorio · índice primario",
                 bootstyle="secondary").pack(anchor="w", pady=(2, 18))

        tarjeta = tb.Labelframe(marco, text="Estado del archivo", padding=16)
        tarjeta.pack(fill=X)
        self.lbl_ruta = tb.Label(tarjeta, text="", justify="left", wraplength=640)
        self.lbl_ruta.pack(anchor="w")
        self.lbl_total = tb.Label(tarjeta, text="", font=("", 11, "bold"))
        self.lbl_total.pack(anchor="w", pady=(8, 0))
        self.lbl_integridad = tb.Label(tarjeta, text="")
        self.lbl_integridad.pack(anchor="w", pady=(4, 0))

        tb.Button(marco, text="Abrir contactos", bootstyle="primary",
                  command=lambda: self.abrir_contactos("agregar")).pack(anchor="w", pady=(18, 0))

    # -- estado -------------------------------------------------------------
    def refrescar_estado(self):
        self.lbl_ruta.configure(text=f"Archivo de datos:\n{self.store.dat_path}")
        self.lbl_total.configure(text=f"Contactos en el índice: {self.store.count()}")
        try:
            rep = self.store.verify_integrity()
            iconos = {IntegrityStatus.OK: "✔", IntegrityStatus.SIN_ARCHIVO: "•"}
            self.lbl_integridad.configure(
                text=f"{iconos.get(rep.estado, '⚠')}  {rep.mensaje}",
                bootstyle="success" if rep.ok else ("secondary" if rep.estado == IntegrityStatus.SIN_ARCHIVO else "danger"),
            )
        except StoreError as exc:
            self.lbl_integridad.configure(text=f"⚠  {exc}", bootstyle="danger")

    # -- menú Contactos -----------------------------------------------------
    def abrir_contactos(self, modo: str = "agregar"):
        if self.contactos_win is None or not self.contactos_win.winfo_exists():
            self.contactos_win = ContactsWindow(self)
        self.contactos_win.enfocar(modo)

    # -- menú Exportar ------------------------------------------------------
    @manejar_errores("Error al exportar")
    def exportar_json(self):
        self.store.exports_dir.mkdir(parents=True, exist_ok=True)
        ruta = filedialog.asksaveasfilename(
            parent=self, title="Exportar a JSON", defaultextension=".json",
            initialdir=self.store.exports_dir, initialfile="contactos_export.json",
            filetypes=[("JSON", "*.json")])
        if not ruta:
            return
        destino, total = self.store.export_json(Path(ruta))
        messagebox.showinfo("Exportación completa",
                            f"Se exportaron {total} contactos a:\n{destino}", parent=self)

    @manejar_errores("Error al generar el PDF")
    def generar_pdf(self):
        self.store.exports_dir.mkdir(parents=True, exist_ok=True)
        ruta = filedialog.asksaveasfilename(
            parent=self, title="Generar reporte PDF", defaultextension=".pdf",
            initialdir=self.store.exports_dir, initialfile="reporte_contactos.pdf",
            filetypes=[("PDF", "*.pdf")])
        if not ruta:
            return
        pdf = Path(ruta)
        json_ruta, pdf_ruta, total = self.store.generate_report(pdf, pdf.with_suffix(".json"))
        messagebox.showinfo(
            "Reporte generado",
            f"{total} contactos.\n\nJSON: {json_ruta}\nPDF: {pdf_ruta}", parent=self)

    # -- menú Herramientas --------------------------------------------------
    @manejar_errores("Error de integridad")
    def verificar_integridad(self):
        rep = self.store.verify_integrity()
        if rep.ok:
            messagebox.showinfo("Integridad", f"{rep.mensaje}\n\nSHA-256:\n{rep.hash_actual}", parent=self)
        elif rep.estado == IntegrityStatus.SIN_ARCHIVO:
            messagebox.showinfo("Integridad", rep.mensaje, parent=self)
        else:
            detalle = rep.mensaje
            if rep.hash_guardado:
                detalle += f"\n\nHash guardado:\n{rep.hash_guardado}"
            if rep.hash_actual:
                detalle += f"\n\nHash actual:\n{rep.hash_actual}"
            detalle += ("\n\n¿Desea aceptar el archivo tal como está? "
                        "Se reconstruirá el índice y se renovará el hash.")
            if messagebox.askyesno("Integridad", detalle, icon="warning", parent=self):
                total = self.store.accept_current_state()
                messagebox.showinfo("Integridad", f"Estado aceptado ({total} registros). Hash renovado.", parent=self)
        self.refrescar_estado()
        if self.contactos_win is not None and self.contactos_win.winfo_exists():
            self.contactos_win.refrescar_tabla()

    @manejar_errores("Error al crear el respaldo")
    def crear_respaldo(self):
        r = self.store.create_backup()
        messagebox.showinfo(
            "Respaldo creado",
            f"Archivo: {r.ruta}\nContiene: {', '.join(r.archivos)}\n\n"
            f"Tamaño original:     {formato_bytes(r.tamano_original)}\n"
            f"Tamaño comprimido:   {formato_bytes(r.tamano_comprimido)}\n"
            f"Tamaño del ZIP:      {formato_bytes(r.tamano_zip)}\n"
            f"Reducción:           {r.ahorro_pct:.1f} %",
            parent=self)

    @manejar_errores("Error")
    def mostrar_info(self):
        InfoWindow(self)

    # -- menú Ayuda (simulado) ---------------------------------------------
    def _ayuda_simulada(self):
        messagebox.showinfo("Ayuda", "Esta opción de ayuda es simulada en el laboratorio.", parent=self)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Agenda de Contactos - Laboratorio 2")
    p.add_argument("--data-dir", type=str, default=str(DEFAULT_DATA_DIR),
                   help="Carpeta donde se guardan contactos.dat, indice.json y contactos.sha256.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    MainApp(Path(args.data_dir)).mainloop()


if __name__ == "__main__":
    main(sys.argv[1:])
