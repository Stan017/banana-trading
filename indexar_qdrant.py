"""
indexar_qdrant.py — Indexa archivos SOLO a Qdrant Cloud
════════════════════════════════════════════════════════
Más liviano que indexar.py — no carga ChromaDB local.

Uso:
    python indexar_qdrant.py archivo.txt
    python indexar_qdrant.py carpeta/
    python indexar_qdrant.py --stats
"""

import os, sys, re, hashlib
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

QDRANT_URL        = os.getenv("QDRANT_URL", "")
QDRANT_API_KEY    = os.getenv("QDRANT_API_KEY", "")
QDRANT_COLLECTION = "killaxbt"
CHUNK_SIZE        = 800
CHUNK_OVERLAP     = 150

# ── Conectar Qdrant ──────────────────────────────────────────
print("🔌 Conectando a Qdrant Cloud...")
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=60)
info   = client.get_collection(QDRANT_COLLECTION)
print(f"✅ Qdrant conectado — {info.points_count} chunks actuales")

# ── Embedder ─────────────────────────────────────────────────
print("🔌 Cargando embedder...")
from sentence_transformers import SentenceTransformer
embedder = SentenceTransformer("all-MiniLM-L6-v2")
print("✅ Embedder listo")

# ── Helpers ──────────────────────────────────────────────────
def detectar_idioma(texto: str) -> str:
    es = len(re.findall(r'\b(que|con|para|los|las|una|del|por|como|más)\b', texto.lower()))
    en = len(re.findall(r'\b(the|and|for|with|that|this|from|have|are|not)\b', texto.lower()))
    return "es" if es > en else "en"

def chunk_texto(texto: str, fuente: str) -> list:
    texto  = re.sub(r'\n{3,}', '\n\n', texto.strip())
    texto  = re.sub(r' {2,}', ' ', texto)
    idioma = detectar_idioma(texto)
    chunks = []
    inicio = 0
    total  = len(texto)
    while inicio < total:
        fin = min(inicio + CHUNK_SIZE, total)
        if fin < total:
            for sep in ["\n\n", ".\n", ". ", "\n", " "]:
                pos = texto.rfind(sep, inicio, fin)
                if pos > inicio + CHUNK_SIZE // 2:
                    fin = pos + len(sep)
                    break
        chunk = texto[inicio:fin].strip()
        if len(chunk) > 50:
            chunks.append({"texto": chunk, "fuente": fuente, "idioma": idioma})
        inicio = fin - CHUNK_OVERLAP
    return chunks

def hash_chunk(texto: str) -> str:
    return hashlib.md5(texto.encode("utf-8")).hexdigest()

def indexar_archivo(ruta: Path) -> tuple:
    print(f"\n  📄 {ruta.name}")
    texto = ruta.read_text(encoding="utf-8", errors="replace")
    if not texto.strip():
        print(f"     ⚠️  Archivo vacío")
        return 0, 0

    chunks = chunk_texto(texto, fuente=ruta.stem)
    print(f"     📦 {len(chunks)} chunks generados — embediendo en batches...")

    puntos  = []
    nuevos  = 0

    # Procesar en batches de 20 para no explotar la RAM
    BATCH = 20
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i:i+BATCH]
        textos_batch = [c["texto"] for c in batch]
        vectores     = embedder.encode(textos_batch, batch_size=8, show_progress_bar=False)

        for chunk, vector in zip(batch, vectores):
            chunk_id    = hash_chunk(chunk["texto"])
            id_numerico = abs(hash(chunk_id)) % (2**63)
            puntos.append(PointStruct(
                id      = id_numerico,
                vector  = vector.tolist(),
                payload = {
                    "text":    chunk["texto"],
                    "fuente":  chunk["fuente"],
                    "idioma":  chunk["idioma"],
                    "archivo": ruta.name,
                    "id_orig": chunk_id,
                }
            ))
            nuevos += 1

        print(f"     ⏳ {min(i+BATCH, len(chunks))}/{len(chunks)} chunks procesados", end="\r")

    # Subir a Qdrant en batches de 50
    for i in range(0, len(puntos), 50):
        client.upsert(
            collection_name=QDRANT_COLLECTION,
            points=puntos[i:i+50],
            wait=True
        )

    idioma = detectar_idioma(texto)
    print(f"     ✅ {nuevos} chunks subidos a Qdrant | idioma: {idioma}        ")
    return nuevos, 0


def main():
    args = sys.argv[1:]

    if "--stats" in args:
        info = client.get_collection(QDRANT_COLLECTION)
        print(f"\n📊 Qdrant Cloud: {info.points_count} chunks\n")
        return

    # Archivo único
    if args and not args[0].startswith("--"):
        ruta = Path(args[0])
        if ruta.is_file():
            nuevos, _ = indexar_archivo(ruta)
            info = client.get_collection(QDRANT_COLLECTION)
            print(f"\n✅ Total Qdrant: {info.points_count} chunks")
            return
        elif ruta.is_dir():
            archivos = [f for f in ruta.iterdir() if f.suffix.lower() in (".txt", ".md")]
        else:
            print(f"❌ No encontrado: {ruta}")
            sys.exit(1)
    else:
        # Carpeta por defecto
        carpeta  = Path(r"C:\Users\stanley\Desktop\copy\document\actualizaciones")
        archivos = [f for f in carpeta.iterdir() if f.suffix.lower() in (".txt", ".md")]

    if not archivos:
        print("⚠️  No hay archivos .txt o .md")
        return

    print(f"\n📁 {len(archivos)} archivos a indexar\n")
    total = 0
    for archivo in sorted(archivos):
        nuevos, _ = indexar_archivo(archivo)
        total += nuevos

    info = client.get_collection(QDRANT_COLLECTION)
    print(f"\n{'═'*50}")
    print(f"✅ LISTO — {total} chunks nuevos")
    print(f"☁️  Qdrant Cloud: {info.points_count} chunks totales")
    print(f"{'═'*50}\n")


if __name__ == "__main__":
    main()
