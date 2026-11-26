FROM python:3.12-alpine@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
WORKDIR /app
ENV PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt ./
RUN apk add --no-cache 'krb5-libs>=1.22.3-r0' 'libcrypto3>=3.5.9-r1' 'libssl3>=3.5.9-r1' 'zlib>=1.4-r0' \
    && python -m pip install --no-cache-dir --upgrade 'pip==26.3' \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip uninstall --yes pip \
    && rm -f /usr/local/lib/python3.12/ensurepip/_bundled/*.whl \
    && addgroup -g 10001 -S app \
    && adduser -u 10001 -S -D -H -G app app \
    && mkdir -p /events \
    && chown app:app /events \
    && python -c "import sqlite3; conn = sqlite3.connect(':memory:'); conn.execute(\"CREATE VIRTUAL TABLE probe USING fts5(name, tokenize='trigram')\")"
COPY app.py search.py search_queue.py ingest.py improvements.py recommendations.py ./
COPY web ./web
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD ["python", "-c", "from http.client import HTTPConnection; c = HTTPConnection('127.0.0.1', 8000, timeout=2); c.request('GET', '/health'); raise SystemExit(c.getresponse().status != 200)"]
CMD ["python", "app.py", "--host", "0.0.0.0", "--db", "/catalog/catalog.sqlite", "--events", "/events/events.sqlite"]
