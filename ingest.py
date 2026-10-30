import os
import tempfile
from supabase import create_client, Client
from sqlalchemy import create_engine, text
from langchain_community.document_loaders import TextLoader, PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.embeddings import FastEmbedEmbeddings
from langchain_postgres import PGVector
from dotenv import load_dotenv

load_dotenv()

CONNECTION_STRING = os.environ.get("SUPABASE_DB_URL")
COLLECTION_NAME = "smartrma_manuals"
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BUCKET_NAME = "manuals"

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100
SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf"}


def delete_existing_chunks(engine, product_name: str) -> int:
    """Remove all chunks previously stored for this product.

    Makes re-running ingestion idempotent: no duplicates, and no stale
    chunks left over if a manual got shorter.
    """
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                DELETE FROM langchain_pg_embedding
                WHERE collection_id = (
                    SELECT uuid FROM langchain_pg_collection WHERE name = :collection
                )
                AND cmetadata->>'product' = :product
                """
            ),
            {"collection": COLLECTION_NAME, "product": product_name},
        )
        return result.rowcount


def ingest_all():
    print("Scanning Supabase Storage bucket for product manuals...")
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Local FastEmbed model (ONNX, runs on CPU): no API limits, no embedding cost
    print("Loading local FastEmbed embedding model...")
    embeddings = FastEmbedEmbeddings(model_name="BAAI/bge-small-en-v1.5")

    vectorstore = PGVector(
        embeddings=embeddings,
        collection_name=COLLECTION_NAME,
        connection=CONNECTION_STRING,
        use_jsonb=True,
    )
    engine = create_engine(CONNECTION_STRING)

    files = supabase.storage.from_(BUCKET_NAME).list()
    if not files:
        print("No files found in the storage bucket.")
        return

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
    )
    summary = {}

    for file_obj in files:
        file_name = file_obj["name"]
        file_ext = os.path.splitext(file_name)[1].lower()

        # Skip hidden files, folders and unsupported types
        if file_name.startswith(".") or file_ext not in SUPPORTED_EXTENSIONS:
            print(f"Skipping {file_name}")
            continue

        product_name = os.path.splitext(file_name)[0].lower()
        print(f"\nProcessing manual for product: [{product_name}] (File: {file_name})")

        temp_path = None
        try:
            res = supabase.storage.from_(BUCKET_NAME).download(file_name)
            with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext, mode="wb") as tmp:
                tmp.write(res)
                temp_path = tmp.name

            loader = TextLoader(temp_path) if file_ext in {".txt", ".md"} else PyPDFLoader(temp_path)
            docs = loader.load()

            # Metadata on every chunk. "source" is overwritten with the real
            # file name (the loader would otherwise store the temp file path).
            for doc in docs:
                doc.metadata["product"] = product_name
                doc.metadata["source"] = file_name

            chunks = text_splitter.split_documents(docs)
            if not chunks:
                print("  -> No text extracted (scanned PDF?). Skipping.")
                continue

            # Replace any previous version of this manual
            removed = delete_existing_chunks(engine, product_name)
            if removed:
                print(f"  -> Removed {removed} old chunks")

            ids = [f"{product_name}-{i}" for i in range(len(chunks))]
            print(f"  -> Generating and storing embeddings for {len(chunks)} chunks...")
            vectorstore.add_documents(chunks, ids=ids)

            summary[product_name] = len(chunks)
            print(f"Successfully embedded manual for [{product_name}]!")

        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

    print("\n--- Ingestion summary ---")
    for product, count in summary.items():
        print(f"  {product}: {count} chunks")
    print(f"Manuals: {len(summary)} | Total chunks: {sum(summary.values())}")
    print(f"(chunk_size={CHUNK_SIZE}, overlap={CHUNK_OVERLAP})")


if __name__ == "__main__":
    ingest_all()
