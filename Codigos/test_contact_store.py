"""Pruebitas y librerias"""
 
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from contact_store import (
    RECORD_SIZE, Contact, ContactStore, ContactNotFoundError, ContactValidationError,
    DataFileCorruptError, DuplicateIdError, IntegrityStatus, StoreError,
    decode_record, encode_record,
)


def nuevo_store(tmp: str) -> ContactStore:
    s = ContactStore(Path(tmp) / "data")
    s.initialize()
    return s


def contacto(i: int, **kw) -> Contact:
    base = dict(id=i, nombre=f"Persona {i}", telefono="5555-1234",
                correo=f"p{i}@correo.com", ciudad="Xela")
    base.update(kw)
    return Contact(**base)


class TestRegistro(unittest.TestCase):
    def test_tamano_fijo(self):
        self.assertEqual(len(encode_record(contacto(1))), RECORD_SIZE)
        self.assertEqual(len(encode_record(contacto(999999, nombre="x" * 30))), RECORD_SIZE)

    def test_utf8_mantiene_tamano_y_ida_vuelta(self):
        c = contacto(7, nombre="José Ñandú Peña", ciudad="Quetzaltenango")
        raw = encode_record(c)
        self.assertEqual(len(raw), RECORD_SIZE)
        self.assertEqual(decode_record(raw), c)

    def test_campo_muy_largo_se_rechaza(self):
        with self.assertRaises(ContactValidationError):
            contacto(1, nombre="ñ" * 20).validar()  # 40 bytes > 30

    def test_validaciones(self):
        for malo in (contacto(0), contacto(1, nombre=""), contacto(1, correo="sin-arroba"),
                     contacto(1, telefono="abc"), contacto(1, nombre="a\nb")):
            with self.assertRaises(ContactValidationError):
                malo.validar()


class TestAccesoAleatorio(unittest.TestCase):
    def test_agregar_buscar_modificar(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            for i in (10, 20, 30):
                s.add(contacto(i))
            self.assertEqual(s.dat_path.stat().st_size, 3 * RECORD_SIZE)
            c, traza = s.get(20)
            self.assertEqual(c.nombre, "Persona 20")
            self.assertEqual(traza.offset, 1 * RECORD_SIZE)
            self.assertEqual(traza.tell_antes, RECORD_SIZE)
            self.assertEqual(traza.tell_despues, 2 * RECORD_SIZE)

            antes = s.dat_path.read_bytes()
            s.update(contacto(20, nombre="Nuevo Nombre"))
            despues = s.dat_path.read_bytes()
            self.assertEqual(len(antes), len(despues))
            # solo cambió el registro #1
            self.assertEqual(antes[:RECORD_SIZE], despues[:RECORD_SIZE])
            self.assertNotEqual(antes[RECORD_SIZE:2 * RECORD_SIZE], despues[RECORD_SIZE:2 * RECORD_SIZE])
            self.assertEqual(antes[2 * RECORD_SIZE:], despues[2 * RECORD_SIZE:])
            self.assertEqual(s.get(20)[0].nombre, "Nuevo Nombre")

    def test_id_duplicado_y_no_encontrado(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            s.add(contacto(1))
            with self.assertRaises(DuplicateIdError):
                s.add(contacto(1))
            with self.assertRaises(ContactNotFoundError):
                s.get(99)
            with self.assertRaises(ContactNotFoundError):
                s.update(contacto(99))

    def test_buscar_sin_archivo_no_se_cae(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ContactNotFoundError):
                nuevo_store(tmp).get(1)
            self.assertEqual(nuevo_store(tmp).list_all(), [])


class TestIndice(unittest.TestCase):
    def test_indice_se_actualiza_al_agregar(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            s.add(contacto(5))
            s.add(contacto(3))
            datos = json.loads(s.index_path.read_text(encoding="utf-8"))
            self.assertEqual(datos["indice"], {"5": 0, "3": 1})

    def test_indice_ausente_o_corrupto_se_reconstruye(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            for i in (1, 2, 3):
                s.add(contacto(i))
            for dano in (None, "{ esto no es json", '{"version": 1, "indice": {"1": 0}}'):
                if dano is None:
                    s.index_path.unlink()
                else:
                    s.index_path.write_text(dano, encoding="utf-8")
                s2 = ContactStore(s.data_dir)
                rep = s2.initialize()
                self.assertTrue(rep.avisos)
                self.assertEqual(s2.get(3)[0].id, 3)
                self.assertEqual(s2.count(), 3)

    def test_indice_desactualizado_detectado_en_busqueda(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            s.add(contacto(1))
            s.add(contacto(2))
            s._index = {1: 1, 2: 0}  # índice mentiroso en memoria
            self.assertEqual(s.get(1)[0].id, 1)


class TestIntegridad(unittest.TestCase):
    def test_hash_ok_y_modificacion_detectada(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            s.add(contacto(1))
            self.assertTrue(s.verify_integrity().ok)
            raw = bytearray(s.dat_path.read_bytes())
            raw[10] = ord("X")  # alteración manual
            s.dat_path.write_bytes(bytes(raw))
            self.assertEqual(s.verify_integrity().estado, IntegrityStatus.MODIFICADO)
            rep = ContactStore(s.data_dir).initialize()
            self.assertEqual(rep.integridad.estado, IntegrityStatus.MODIFICADO)
            self.assertTrue(any("modificado" in a for a in rep.avisos))
            s.accept_current_state()
            self.assertTrue(s.verify_integrity().ok)

    def test_sin_hash_y_hash_danado(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            s.add(contacto(1))
            s.hash_path.write_text("basura", encoding="utf-8")
            self.assertEqual(s.verify_integrity().estado, IntegrityStatus.HASH_INVALIDO)
            s.hash_path.unlink()
            self.assertEqual(s.verify_integrity().estado, IntegrityStatus.SIN_HASH)
            ContactStore(s.data_dir).initialize()
            self.assertTrue(s.hash_path.exists())

    def test_archivo_dat_corrupto_no_se_cae(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            s.add(contacto(1))
            with open(s.dat_path, "ab") as f:
                f.write(b"basura")
            s2 = ContactStore(s.data_dir)
            rep = s2.initialize()
            self.assertTrue(rep.avisos)
            with self.assertRaises(DataFileCorruptError):
                s2.add(contacto(2))
            with self.assertRaises(ContactNotFoundError):
                s2.get(1)

    def test_dat_con_texto_invalido(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            s.dat_path.write_bytes(b"x" * RECORD_SIZE)
            s2 = ContactStore(s.data_dir)
            s2.initialize()
            with self.assertRaises(StoreError):
                s2.list_all()


class TestRespaldoExportMeta(unittest.TestCase):
    def test_respaldo_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            for i in range(1, 51):
                s.add(contacto(i))
            r = s.create_backup()
            self.assertTrue(r.ruta.exists())
            with zipfile.ZipFile(r.ruta) as z:
                self.assertEqual(sorted(z.namelist()), ["contactos.dat", "indice.json"])
                self.assertIsNone(z.testzip())
            self.assertGreater(r.tamano_original, r.tamano_zip)
            self.assertEqual(r.tamano_original, s.dat_path.stat().st_size + s.index_path.stat().st_size)

    def test_respaldo_sin_datos(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(StoreError):
                nuevo_store(tmp).create_backup()

    def test_export_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            s.add(contacto(1, nombre="Ñoño Pérez"))
            ruta, total = s.export_json()
            datos = json.loads(ruta.read_text(encoding="utf-8"))
            self.assertEqual(total, 1)
            self.assertEqual(datos["contactos"][0]["nombre"], "Ñoño Pérez")
            self.assertIn("generado", datos)

    def test_reporte_pdf(self):
        try:
            import fpdf  # noqa: F401
        except ImportError:
            self.skipTest("fpdf2 no instalado")
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            for i in range(1, 80):
                s.add(contacto(i, nombre=f"José Peña {i}"))
            j, p, total = s.generate_report()
            self.assertEqual(total, 79)
            self.assertTrue(p.read_bytes().startswith(b"%PDF"))

    def test_pdf_desde_json_invalido(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            malo = Path(tmp) / "malo.json"
            malo.write_text("{{{", encoding="utf-8")
            with self.assertRaises(StoreError):
                s.export_pdf_from_json(malo)

    def test_metadatos_y_permisos(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            s.add(contacto(1))
            info = {i.nombre: i for i in s.file_info()}
            dat = info["contactos.dat"]
            self.assertTrue(dat.existe and dat.lectura and dat.escritura)
            self.assertEqual(dat.tamano, RECORD_SIZE)
            self.assertIsNotNone(dat.modificado)
            s.dat_path.unlink()
            self.assertFalse({i.nombre: i for i in s.file_info()}["contactos.dat"].existe)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "los permisos no se pueden probar como root/Windows")
    def test_sin_permisos(self):
        from contact_store import StorePermissionError
        with tempfile.TemporaryDirectory() as tmp:
            s = nuevo_store(tmp)
            s.add(contacto(1))
            os.chmod(s.dat_path, 0o000)
            try:
                with self.assertRaises(StorePermissionError):
                    s.get(1)
            finally:
                os.chmod(s.dat_path, 0o644)


if __name__ == "__main__":
    unittest.main()
