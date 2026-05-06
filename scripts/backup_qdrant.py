"""
backup_qdrant.py — Exporta todos los chunks de Qdrant Cloud a un archivo local.
Uso: python backup_qdrant.py
Genera: qdrant_backup_YYYY-MM-DD.json
"""

import os
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

QDRANT_URL        = os.getenv("QDRANT_URL", "")
QDRANT_API_KEY    = os.getenv("QDRANT_API_KEY", "")
QDRANT_COLLECTION = "killaxbt"

if not QDRANT_URL or not QDRANT_API_KEY:
    print("ERROR: QDRANT_URL o QDRANT_API_KEY no están en el .env")
    exit(1)

from qdrant_client import QdrantClient

client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)

# Info de la colección
info = client.get_collection(QDRANT_COLLECTION)
total = info.points_count
print(f"Colección: {QDRANT_COLLECTION}")
print(f"Total de puntos: {total}")
print("Descargando...", flush=True)

puntos = []
offset = None
batch  = 500

while True:
    resultado = client.scroll(
        collection_name=QDRANT_COLLECTION,
        limit=batch,
        offset=offset,
        with_payload=True,
        with_vectors=False,   # no necesitamos los embeddings para el backup de texto
    )
    records, next_offset = resultado

    if not records:
        break

    for r in records:
        puntos.append({
            "id":      r.id,
            "payload": r.payload,
        })

    print(f"  {len(puntos)}/{total}", end="\r", flush=True)

    if next_offset is None:
        break
    offset = next_offset

print(f"\nDescargados: {len(puntos)} puntos")

# Guardar
fecha      = datetime.now().strftime("%Y-%m-%d")
out_path   = os.path.join(os.path.dirname(__file__), f"qdrant_backup_{fecha}.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(puntos, f, ensure_ascii=False, indent=2)

size_mb = os.path.getsize(out_path) / 1024 / 1024
print(f"Guardado en: {out_path}  ({size_mb:.1f} MB)")
