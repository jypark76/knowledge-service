# In plain English: this is the recipe that packs the knowledge service into one
# sealed box (a Docker image) that runs the same way on any computer.
# Build it from the repo folder:  docker build -t knowledge-service .
# Run it:                         docker run --rm -p 8000:8000 knowledge-service

# Start from a small, ready-made Python system.
FROM python:3.12-slim

# Python settings for containers: do not leave stray cache files, and print
# messages straight away so they show up in the logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Work inside a folder called /app in the box.
WORKDIR /app

# Install the libraries first. Docker remembers this step, so rebuilding is
# fast as long as requirements.txt has not changed. requirements.txt is a lock file: every library
# (and every library those need) is pinned to one version and its file hashes. --require-hashes makes
# the build FAIL if any downloaded file differs by even one byte, and --no-deps stops pip from
# fetching anything the lock does not list.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.txt

# Never run the service as the all-powerful root user inside the box.
RUN useradd --create-home appuser

# Save the AI model's files inside the image while it is being built, so the
# service never has to download them (about 130 MB) when it starts. The model
# lives in /app/model_cache, which the service's own user owns.
ENV FASTEMBED_CACHE_PATH=/app/model_cache
RUN mkdir /app/model_cache && chown appuser /app/model_cache
USER appuser
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

# Check the downloaded model files against hashes written in this repo (model/model.sha256). If a
# file is not exactly the one we pinned, or the model's version on the download site has changed,
# the build FAILS here instead of quietly packing unknown files into the image. To move to a new
# model version on purpose, change the pin file and the folder name below in the same pull request.
COPY --chown=appuser model/model.sha256 /tmp/model.sha256
RUN cd /app/model_cache/models--Qdrant--bge-small-en-v1.5-onnx-Q/snapshots/aa8f8b060edb00e03bfdd08813a2949946c8ba55 \
    && sha256sum -c /tmp/model.sha256

# Copy in the service code.
COPY --chown=appuser app/ ./app/

# The service listens on port 8000.
EXPOSE 8000

# The command that starts the service when the box runs. 0.0.0.0 means "accept
# connections from outside the box", which is needed for port forwarding.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
