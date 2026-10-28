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