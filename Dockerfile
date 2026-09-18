ARG PYTHON_VERSION=3.14
FROM python:${PYTHON_VERSION}-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends pandoc ca-certificates \
  && rm -rf /var/lib/apt/lists/*

ARG FLAVOR=small

# Aspose.Slides ships a bundled .NET runtime; slim images need GDI+/fonts.
# Invariant globalization avoids ICU-major mismatches on Debian 12+.
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1

RUN if [ "$FLAVOR" = "small" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends \
        libgdiplus \
        libfontconfig1 \
        fonts-dejavu-core \
      && rm -rf /var/lib/apt/lists/*; \
    fi

RUN if [ "$FLAVOR" = "gpt" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr \
        poppler-utils \
        libcairo2 \
        libcairo2-dev \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libpangoft2-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libffi-dev \
        shared-mime-info \
        libsndfile1 \
        libgl1 \
        libglib2.0-0 \
        libzbar0 \
        graphviz \
        libgraphviz-dev \
        pkg-config \
        gcc \
        g++ \
        gfortran \
        cmake \
        libopenblas-dev \
        liblapack-dev \
        libhdf5-dev \
        default-jre-headless \
        fonts-liberation \
        fonts-dejavu-core \
        git \
        libxml2 \
        libxslt1.1 \
      && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app
COPY requirements-server.txt requirements-small.txt requirements-gpt.txt ./

RUN pip install --no-cache-dir -r requirements-server.txt

RUN case "$FLAVOR" in \
      zero) ;; \
      small) \
        pip install --no-cache-dir -r requirements-small.txt ;; \
      gpt) \
        pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
          torch torchaudio torchvision \
        && pip freeze | grep -E '^(torch|torchaudio|torchvision)==' > /tmp/torch.constraints \
        && pip install --no-cache-dir --prefer-binary \
             -c /tmp/torch.constraints \
             -r requirements-gpt.txt ;; \
      *) echo "unknown FLAVOR=$FLAVOR (expected zero|small|gpt)" >&2; exit 1 ;; \
    esac \
  && pip install --no-cache-dir -r requirements-server.txt

COPY src ./src
ENV PYTHONPATH=/app/src
ENV SANDBOX_DATA=/data
ENV MPLBACKEND=Agg
ENV SANDBOX_FLAVOR=${FLAVOR}

RUN useradd --create-home --uid 1000 sandbox \
  && mkdir -p /data \
  && chown sandbox:sandbox /data \
  && chown -R root:root /app \
  && chmod -R go-w /app

USER sandbox
EXPOSE 8090

HEALTHCHECK --interval=15s --timeout=5s --retries=10 --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health')"

CMD ["uvicorn", "sandbox.main:app", "--host", "0.0.0.0", "--port", "8090"]
