import os
import chromadb
import PyPDF2
from chromadb.utils import embedding_functions

# ============================================================
# CONFIGURACIÓN
# ============================================================
_BASE = os.path.dirname(__file__)
CARPETA_DOCS = os.path.join(_BASE, "document", "actualizaciones")
CARPETA_DB   = os.path.join(_BASE, "base_conocimiento")

# ============================================================
# CONEXIÓN A LA BASE DE CONOCIMIENTO
# ============================================================
print("🚀 Iniciando base de conocimiento...")
client = chromadb.PersistentClient(path=CARPETA_DB)

embedding_fn = embedding_functions.DefaultEmbeddingFunction()

coleccion = client.get_or_create_collection(
    name="killaxbt",
    embedding_function=embedding_fn
)

# ============================================================
# FUNCIONES PARA LEER ARCHIVOS
# ============================================================
def leer_txt(ruta):
    with open(ruta, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()

def leer_pdf(ruta):
    texto = ""
    with open(ruta, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        for pagina in reader.pages:
            texto += pagina.extract_text() or ""
    return texto

def dividir_en_chunks(texto, chunk_size=500):
    """Divide el texto en pedazos de ~500 caracteres para el RAG"""
    palabras = texto.split()
    chunks = []
    chunk_actual = []
    contador = 0

    for palabra in palabras:
        chunk_actual.append(palabra)
        contador += len(palabra)
        if contador >= chunk_size:
            chunks.append(" ".join(chunk_actual))
            chunk_actual = []
            contador = 0

    if chunk_actual:
        chunks.append(" ".join(chunk_actual))

    return chunks

# ============================================================
# PROCESAR TODOS LOS ARCHIVOS
# ============================================================
archivos = os.listdir(CARPETA_DOCS)
total = 0

print(f"📂 Encontrados {len(archivos)} archivos en la carpeta\n")

for archivo in archivos:
    ruta = os.path.join(CARPETA_DOCS, archivo)
    texto = ""

    if archivo.endswith(".txt"):
        print(f"📄 Procesando TXT: {archivo}")
        texto = leer_txt(ruta)

    elif archivo.endswith(".pdf"):
        print(f"📕 Procesando PDF: {archivo}")
        texto = leer_pdf(ruta)

    else:
        print(f"⏭️  Saltando: {archivo}")
        continue

    if not texto.strip():
        print(f"   ⚠️  Archivo vacío, saltando...\n")
        continue

    # Dividir en chunks
    chunks = dividir_en_chunks(texto)

    # Agregar a ChromaDB
    coleccion.add(
        documents=chunks,
        ids=[f"{archivo}_chunk_{i}" for i in range(len(chunks))],
        metadatas=[{"fuente": archivo} for _ in chunks]
    )

    total += len(chunks)
    print(f"   ✅ {len(chunks)} chunks agregados\n")

print(f"🎉 ¡Listo! Base de conocimiento creada con {total} chunks")
print(f"📍 Guardada en: {CARPETA_DB}")
