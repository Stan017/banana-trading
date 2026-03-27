"""
indexar.py — Pipeline de ingesta para TradeBot KB
═══════════════════════════════════════════════════════════════
Uso:
    python indexar.py                  → indexa todos los archivos de la carpeta
    python indexar.py archivo.txt      → indexa un archivo específico
    python indexar.py --stats          → muestra estadísticas de la KB actual

Formatos soportados: .txt  .md  .pdf

Indexa en DOS destinos simultáneamente:
    ✅ ChromaDB local  — backup en tu máquina
    ✅ Qdrant Cloud    — producción en Railway

Workflow:
    1. Pon tus archivos en la carpeta de ingesta (ver abajo)
    2. Corre: python indexar.py
    3. El script chunkea, deduplica y añade a ambas KBs
    4. Llama a /reload-kb para que Flask actualice el conteo
"""

import os
import sys
import hashlib
import re
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ════════════════════════════════════════════════════════
# CONFIGURACIÓN — ajusta estas rutas si es necesario
# ════════════════════════════════════════════════════════

# ── Ruta de ChromaDB local (backup)
KB_PATH = os.getenv("KB_PATH", r"C:\Users\stanley\Desktop\copy\base_conocimiento")

# ── Qdrant Cloud (producción)
QDRANT_URL        = os.getenv("QDRANT_URL", "")
QDRANT_API_KEY    = os.getenv("QDRANT_API_KEY", "")
QDRANT_COLLECTION = "killaxbt"
VECTOR_SIZE       = 384   # dimensión all-MiniLM-L6-v2

# ── Aquí va la carpeta con los archivos a indexar ──────
CARPETA_INGESTA = r"C:\Users\stanley\Desktop\copy\document\actualizaciones"
# ───────────────────────────────────────────────────────
# Ejemplo: r"C:\Users\stanley\Desktop\nuevos_contenidos"

# ── Nombre de la colección en ChromaDB
COLECCION_NOMBRE = "killaxbt"

# ── Tamaño de chunks
CHUNK_SIZE    = 800
CHUNK_OVERLAP = 150

# ════════════════════════════════════════════════════════
# CONEXIONES
# ════════════════════════════════════════════════════════

# ── ChromaDB local ───────────────────────────────────────
print(f"\n🔌 Conectando a ChromaDB local...")
import chromadb
from chromadb.utils import embedding_functions

client_db    = chromadb.PersistentClient(path=KB_PATH)
embedding_fn = embedding_functions.DefaultEmbeddingFunction()
coleccion    = client_db.get_or_create_collection(
    name=COLECCION_NOMBRE,
    embedding_function=embedding_fn
)
print(f"✅ ChromaDB local — {coleccion.count()} chunks")

# ── Qdrant Cloud ─────────────────────────────────────────
_qdrant_client  = None
_qdrant_ok      = False
_embedder       = None

if QDRANT_URL and QDRANT_API_KEY:
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import PointStruct
        from sentence_transformers import SentenceTransformer

        _qdrant_client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=30)
        _qdrant_client.get_collections()  # test conexión
        info = _qdrant_client.get_collection(QDRANT_COLLECTION)
        _embedder  = SentenceTransformer("all-MiniLM-L6-v2")
        _qdrant_ok = True
        print(f"✅ Qdrant Cloud     — {info.points_count} chunks")
    except ImportError:
        print("⚠️  qdrant-client o sentence-transformers no instalado — solo indexará ChromaDB")
    except Exception as e:
        print(f"⚠️  Qdrant no disponible ({e}) — solo indexará ChromaDB local")
else:
    print("⚠️  QDRANT_URL/QDRANT_API_KEY no definidos — solo indexará ChromaDB local")

print()

# ════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════

def detectar_idioma(texto: str) -> str:
    texto_lower = texto.lower()
    palabras_es = ["el ", "la ", "los ", "las ", "que ", "con ", "para ", "una ", "este ", "como "]
    palabras_en = ["the ", "and ", "for ", "with ", "this ", "that ", "from ", "when ", "price ", "market "]
    score_es = sum(texto_lower.count(p) for p in palabras_es)
    score_en = sum(texto_lower.count(p) for p in palabras_en)
    return "es" if score_es >= score_en else "en"


def chunk_texto(texto: str, fuente: str) -> list[dict]:
    texto  = re.sub(r'\n{3,}', '\n\n', texto.strip())
    texto  = re.sub(r' {2,}', ' ', texto)
    chunks = []
    inicio = 0
    total  = len(texto)
    idioma = detectar_idioma(texto)

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


def leer_archivo(ruta: Path) -> str:
    ext = ruta.suffix.lower()
    if ext in (".txt", ".md"):
        try:
            return ruta.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ruta.read_text(encoding="latin-1")
    elif ext == ".pdf":
        try:
            import pdfplumber
            texto = ""
            with pdfplumber.open(str(ruta)) as pdf:
                for pagina in pdf.pages:
                    texto += (pagina.extract_text() or "") + "\n"
            return texto
        except ImportError:
            print("  ⚠️  Para PDFs: pip install pdfplumber")
            return ""
        except Exception as e:
            print(f"  ❌ Error leyendo PDF: {e}")
            return ""
    return ""


def obtener_ids_chroma() -> set:
    try:
        return set(coleccion.get(include=[])["ids"])
    except Exception:
        return set()


def obtener_ids_qdrant() -> set:
    """Retorna set vacío — upsert de Qdrant maneja duplicados automáticamente"""
    if not _qdrant_ok or not _qdrant_client:
        return set()
    return set()


# ════════════════════════════════════════════════════════
# INDEXAR UN ARCHIVO
# ════════════════════════════════════════════════════════

def indexar_archivo(
    ruta: Path,
    ids_chroma: set,
    ids_qdrant: set
) -> tuple[int, int]:
    """
    Indexa un archivo en ChromaDB local Y Qdrant Cloud.
    Deduplica contra ambas KBs independientemente.
    Retorna (chunks_nuevos_total, chunks_duplicados_total)
    """
    print(f"  📄 {ruta.name}")
    texto = leer_archivo(ruta)

    if not texto.strip():
        print(f"     ⚠️  Archivo vacío o sin texto extraíble")
        return 0, 0

    chunks     = chunk_texto(texto, fuente=ruta.stem)
    nuevos     = 0
    duplicados = 0

    # Batches para cada destino
    batch_chroma_ids   = []
    batch_chroma_docs  = []
    batch_chroma_metas = []

    batch_qdrant_puntos = []

    for chunk in chunks:
        chunk_id = hash_chunk(chunk["texto"])
        meta     = {
            "fuente":  chunk["fuente"],
            "idioma":  chunk["idioma"],
            "archivo": ruta.name,
        }

        es_nuevo = False

        # ── ChromaDB — añadir si no existe ──
        if chunk_id not in ids_chroma:
            batch_chroma_ids.append(chunk_id)
            batch_chroma_docs.append(chunk["texto"])
            batch_chroma_metas.append(meta)
            ids_chroma.add(chunk_id)
            es_nuevo = True

        # ── Qdrant — añadir si no existe ──
        if _qdrant_ok and chunk_id not in ids_qdrant:
            vector = _embedder.encode(chunk["texto"]).tolist()
            id_numerico = abs(hash(chunk_id)) % (2**63)
            payload = {
                "text":    chunk["texto"],
                "fuente":  chunk["fuente"],
                "idioma":  chunk["idioma"],
                "archivo": ruta.name,
                "id_orig": chunk_id,
            }
            from qdrant_client.models import PointStruct
            batch_qdrant_puntos.append(
                PointStruct(id=id_numerico, vector=vector, payload=payload)
            )
            ids_qdrant.add(chunk_id)
            es_nuevo = True

        if es_nuevo:
            nuevos += 1
        else:
            duplicados += 1

    # ── Subir a ChromaDB ──
    if batch_chroma_ids:
        coleccion.add(
            ids=batch_chroma_ids,
            documents=batch_chroma_docs,
            metadatas=batch_chroma_metas,
        )

    # ── Subir a Qdrant ──
    if batch_qdrant_puntos and _qdrant_ok:
        _qdrant_client.upsert(
            collection_name=QDRANT_COLLECTION,
            points=batch_qdrant_puntos,
            wait=True
        )

    idioma = detectar_idioma(texto)
    destinos = "ChromaDB + Qdrant" if _qdrant_ok else "ChromaDB local"
    print(f"     ✅ {nuevos} chunks nuevos | {duplicados} duplicados omitidos | {idioma} | → {destinos}")
    return nuevos, duplicados


# ════════════════════════════════════════════════════════
# STATS
# ════════════════════════════════════════════════════════

def mostrar_stats():
    print(f"\n📊 ESTADÍSTICAS DE LA KB")
    print(f"   ChromaDB local: {coleccion.count()} chunks")
    if _qdrant_ok:
        try:
            info = _qdrant_client.get_collection(QDRANT_COLLECTION)
            print(f"   Qdrant Cloud:   {info.points_count} chunks")
        except Exception:
            print(f"   Qdrant Cloud:   error al consultar")
    try:
        sample = coleccion.get(limit=min(100, coleccion.count()), include=["metadatas"])
        idiomas = {}
        fuentes = {}
        for meta in sample["metadatas"]:
            lang = meta.get("idioma", "?")
            idiomas[lang] = idiomas.get(lang, 0) + 1
            src = meta.get("fuente", "?")
            fuentes[src] = fuentes.get(src, 0) + 1
        print(f"   Idiomas (muestra): {idiomas}")
        print(f"   Fuentes (top 5):")
        for src, cnt in sorted(fuentes.items(), key=lambda x: -x[1])[:5]:
            print(f"     · {src}: {cnt} chunks")
    except Exception as e:
        print(f"   (No se pudo obtener detalle: {e})")
    print()


# ════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════

def main():
    args = sys.argv[1:]

    if "--stats" in args:
        mostrar_stats()
        return

    # Cargar IDs existentes de ambas KBs para deduplicar
    print("🔍 Cargando IDs existentes para deduplicación...")
    ids_chroma = obtener_ids_chroma()
    ids_qdrant = obtener_ids_qdrant()
    print(f"   ChromaDB: {len(ids_chroma)} IDs | Qdrant: {len(ids_qdrant)} IDs\n")

    # ── Modo archivo único ──
    if args and not args[0].startswith("--"):
        ruta = Path(args[0])
        if not ruta.exists():
            print(f"❌ Archivo no encontrado: {ruta}")
            sys.exit(1)
        nuevos, dupes = indexar_archivo(ruta, ids_chroma, ids_qdrant)
        print(f"\n✅ Listo — {nuevos} chunks añadidos | {dupes} duplicados omitidos")
        print(f"📚 ChromaDB: {coleccion.count()} chunks")
        if _qdrant_ok:
            info = _qdrant_client.get_collection(QDRANT_COLLECTION)
            print(f"☁️  Qdrant:   {info.points_count} chunks")
        print(f"\n💡 Para recargar en Flask:")
        print(f"   curl -X POST http://localhost:5000/reload-kb -H 'Authorization: Bearer TU_ADMIN_TOKEN'")
        return

    # ── Modo carpeta completa ──
    carpeta = Path(CARPETA_INGESTA)
    if not carpeta.exists():
        print(f"❌ Carpeta no encontrada: {carpeta}")
        print(f"   Edita CARPETA_INGESTA en el script")
        sys.exit(1)

    archivos = [
        f for f in carpeta.iterdir()
        if f.is_file() and f.suffix.lower() in (".txt", ".md", ".pdf")
    ]

    if not archivos:
        print(f"⚠️  No hay archivos .txt, .md o .pdf en: {carpeta}")
        return

    print(f"📁 Carpeta: {carpeta}")
    print(f"📄 Archivos encontrados: {len(archivos)}\n")

    total_nuevos = 0
    total_dupes  = 0

    for archivo in sorted(archivos):
        nuevos, dupes = indexar_archivo(archivo, ids_chroma, ids_qdrant)
        total_nuevos += nuevos
        total_dupes  += dupes

    print(f"\n{'═'*55}")
    print(f"✅ INDEXADO COMPLETO")
    print(f"   Chunks nuevos:       {total_nuevos}")
    print(f"   Duplicados omitidos: {total_dupes}")
    print(f"   ChromaDB local:      {coleccion.count()} chunks")
    if _qdrant_ok:
        try:
            info = _qdrant_client.get_collection(QDRANT_COLLECTION)
            print(f"   Qdrant Cloud:        {info.points_count} chunks")
        except Exception:
            pass
    print(f"\n💡 Para recargar en Flask sin reiniciar:")
    print(f"   curl -X POST http://localhost:5000/reload-kb \\")
    print(f"        -H 'Authorization: Bearer TU_ADMIN_TOKEN'")
    print(f"{'═'*55}\n")


if __name__ == "__main__":
    main()

