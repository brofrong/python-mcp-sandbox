FROM python:3.14-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends pandoc \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
ENV PYTHONPATH=/app/src
ENV SANDBOX_DATA=/data
ENV SANDBOX_REQUIRE_ISOLATION=1
ENV MPLBACKEND=Agg

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
