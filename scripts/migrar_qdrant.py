"""
migrar_qdrant.py — Migración de ChromaDB local → Qdrant Cloud
═══════════════════════════════════════════════════════════════
Uso:
    python migrar_qdrant.py

Qué hace:
    1. Lee TODOS los chunks de ChromaDB local
    2. Los sube a Qdrant Cloud en batches de 100
    3. Muestra progreso en tiempo real
    4. Al final verifica que el conteo sea correcto

Requisitos:
    pip install qdrant-client

Variables de entorno necesarias en .env:
    QDRANT_URL=https://99dbcaf3-d466-423e-9156-df916bc804a6.us-east-1-1.aws.cloud.qdrant.io
    QDRANT_API_KEY=tu_api_key_aqui
    KB_PATH=C:\\Users\\stanley\\Desktop\\copy\\base_conocimiento
"""

import os
import sys
import time
from dotenv import load_dotenv

load_dotenv()

# ════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ════════════════════════════════════════════════════════

KB_PATH          = os.getenv("KB_PATH", os.path.join(os.path.dirname(__file__), "base_conocimiento"))
COLECCION_CHROMA = "killaxbt"
COLECCION_QDRANT = "killaxbt"   # mismo nombre en Qdrant
BATCH_SIZE       = 100          # chunks por batch — no saturar la API
VECTOR_SIZE      = 384          # dimensión del embedding de ChromaDB DefaultEmbeddingFunction

QDRANT_URL     = os.getenv("QDRANT_URL", "")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")

if not QDRANT_API_KEY:
    print("❌ QDRANT_API_KEY no está definida en el .env")
    print("   Agrega: QDRANT_API_KEY=tu_api_key")
    sys.exit(1)

# ════════════════════════════════════════════════════════
# CONEXIONES
# ════════════════════════════════════════════════════════

print("\n🔌 Conectando a ChromaDB local...")
try:
    import chromadb
    from chromadb.utils import embedding_functions
    client_chroma = chromadb.PersistentClient(path=KB_PATH)
    embedding_fn  = embedding_functions.DefaultEmbeddingFunction()
    coleccion     = client_chroma.get_collection(
        name=COLECCION_CHROMA,
        embedding_function=embedding_fn
    )
    total_chunks = coleccion.count()
    print(f"✅ ChromaDB conectado — {total_chunks} chunks encontrados")
except Exception as e:
    print(f"❌ Error conectando a ChromaDB: {e}")
    sys.exit(1)

print("\n🔌 Conectando a Qdrant Cloud...")
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance, VectorParams, PointStruct, OptimizersConfigDiff
    )
    client_qdrant = QdrantClient(
        url=QDRANT_URL,
        api_key=QDRANT_API_KEY,
        timeout=60
    )
    # Test de conexión
    client_qdrant.get_collections()
    print(f"✅ Qdrant Cloud conectado — {QDRANT_URL}")
except ImportError:
    print("❌ qdrant-client no instalado. Corre: pip install qdrant-client")
    sys.exit(1)
except Exception as e:
    print(f"❌ Error conectando a Qdrant: {e}")
    sys.exit(1)

# ════════════════════════════════════════════════════════
# CREAR COLECCIÓN EN QDRANT
# ════════════════════════════════════════════════════════

print(f"\n📦 Preparando colección '{COLECCION_QDRANT}' en Qdrant...")

colecciones_existentes = [c.name for c in client_qdrant.get_collections().collections]

if COLECCION_QDRANT in colecciones_existentes:
    respuesta = input(f"⚠️  La colección '{COLECCION_QDRANT}' ya existe en Qdrant. ¿Borrar y recrear? (s/n): ")
    if respuesta.lower() == "s":
        client_qdrant.delete_collection(COLECCION_QDRANT)
        print(f"   🗑️  Colección borrada")
    else:
        print("   ℹ️  Usando colección existente — se añadirán los chunks nuevos")

if COLECCION_QDRANT not in [c.name for c in client_qdrant.get_collections().collections]:
    client_qdrant.create_collection(
        collection_name=COLECCION_QDRANT,
        vectors_config=VectorParams(
            size=VECTOR_SIZE,
            distance=Distance.COSINE   # mismo que ChromaDB por defecto
        ),
        optimizers_config=OptimizersConfigDiff(
            indexing_threshold=0   # indexar inmediatamente
        )
    )
    print(f"✅ Colección '{COLECCION_QDRANT}' creada en Qdrant")

# ════════════════════════════════════════════════════════
# MIGRACIÓN EN BATCHES
# ════════════════════════════════════════════════════════

print(f"\n🚀 Iniciando migración — {total_chunks} chunks en batches de {BATCH_SIZE}")
print(f"   Esto puede tardar 2-5 minutos dependiendo de tu conexión...\n")

migrados    = 0
errores     = 0
offset      = 0

inicio = time.time()

while offset < total_chunks:
    try:
        # Leer batch de ChromaDB
        batch = coleccion.get(
            limit=BATCH_SIZE,
            offset=offset,
            include=["documents", "metadatas", "embeddings"]
        )

        ids        = batch["ids"]
        documentos = batch["documents"]
        metadatas  = batch["metadatas"]
        embeddings = batch["embeddings"]

        if not ids:
            break

        # Construir puntos para Qdrant
        puntos = []
        for i, (doc_id, doc, meta, emb) in enumerate(
            zip(ids, documentos, metadatas, embeddings)
        ):
            # Qdrant necesita IDs numéricos enteros positivos
            # Los IDs de ChromaDB pueden ser cualquier string (hash MD5, nombre de archivo, etc.)
            # Usamos hash() de Python y lo convertimos a positivo con abs()
            id_numerico = abs(hash(doc_id)) % (2**63)  # dentro del rango de int64

            payload = {
                "text":    doc,
                "fuente":  meta.get("fuente", ""),
                "idioma":  meta.get("idioma", ""),
                "archivo": meta.get("archivo", ""),
                "id_orig": doc_id,   # guardamos el ID original por si acaso
            }

            puntos.append(PointStruct(
                id=id_numerico,
                vector=emb,
                payload=payload
            ))

        # Subir batch a Qdrant
        client_qdrant.upsert(
            collection_name=COLECCION_QDRANT,
            points=puntos,
            wait=True
        )

        migrados += len(puntos)
        offset   += len(ids)

        # Progreso
        pct = (migrados / total_chunks) * 100
        elapsed = time.time() - inicio
        rate = migrados / elapsed if elapsed > 0 else 0
        eta = (total_chunks - migrados) / rate if rate > 0 else 0

        print(
            f"  ✅ {migrados:>5}/{total_chunks} chunks "
            f"({pct:5.1f}%) — "
            f"{rate:.0f} chunks/s — "
            f"ETA: {eta:.0f}s"
        )

    except Exception as e:
        errores += 1
        print(f"  ❌ Error en batch offset={offset}: {e}")
        offset += BATCH_SIZE   # saltar batch con error y continuar
        if errores > 5:
            print("  ❌ Demasiados errores — abortando migración")
            sys.exit(1)

# ════════════════════════════════════════════════════════
# VERIFICACIÓN FINAL
# ════════════════════════════════════════════════════════

tiempo_total = time.time() - inicio

print(f"\n{'═'*55}")
print(f"✅ MIGRACIÓN COMPLETADA")
print(f"   Chunks migrados:  {migrados}")
print(f"   Errores:          {errores}")
print(f"   Tiempo total:     {tiempo_total:.1f}s")
print(f"   Velocidad media:  {migrados/tiempo_total:.0f} chunks/s")

# Verificar conteo en Qdrant
try:
    info = client_qdrant.get_collection(COLECCION_QDRANT)
    conteo_qdrant = info.points_count
    print(f"\n📊 Verificación:")
    print(f"   ChromaDB local: {total_chunks} chunks")
    print(f"   Qdrant Cloud:   {conteo_qdrant} puntos")
    if conteo_qdrant >= total_chunks * 0.99:  # 99% tolerancia
        print(f"   ✅ Migración verificada correctamente")
    else:
        print(f"   ⚠️  Diferencia detectada — revisar errores")
except Exception as e:
    print(f"   ⚠️  No se pudo verificar conteo: {e}")

print(f"\n💡 PRÓXIMOS PASOS:")
print(f"   1. Agrega al .env:")
print(f"      QDRANT_URL={QDRANT_URL}")
print(f"      QDRANT_API_KEY=tu_api_key")
print(f"   2. Corre: python migrar_qdrant.py --test")
print(f"      Para verificar que las búsquedas funcionan")
print(f"   3. Actualiza app_flask.py para usar Qdrant")
print(f"{'═'*55}\n")