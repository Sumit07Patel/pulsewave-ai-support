FROM python:3.11-slim

WORKDIR /code
ENV PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_DIR=/code/models

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image so cold starts do not download it.
RUN python -c "import os; from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir=os.environ['FASTEMBED_CACHE_DIR'])"

COPY app ./app
COPY index.html .

# Cloud Run and Render both provide $PORT.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
