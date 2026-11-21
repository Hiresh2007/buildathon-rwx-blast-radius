# syntax=docker/dockerfile:1
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS source
ADD --checksum=sha256:93b2445f15f395b81c20556777ece19a6c7507df950f13fa9fac4a3be739af0d https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/source.tar.gz
RUN mkdir /opt/sqlglot && tar -xzf /tmp/source.tar.gz --strip-components=1 -C /opt/sqlglot \
    && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --disable-pip-version-check --no-cache-dir -r requirements.lock
COPY --from=source /opt/sqlglot /opt/sqlglot
COPY *.py *.json.gz ./
COPY tools/build_children.py /tmp/build_children.py
RUN python /tmp/build_children.py /opt/sqlglot coverage.json.gz children.json.gz
RUN python -I -B -S symbols.py --build-baseline /opt/sqlglot symbols.json.gz symbol-baseline.sqlite3 \
    && chmod 644 symbol-baseline.sqlite3

COPY tools/build_import_blocks.py tools/build_execution.py tools/build_map.py /tmp/rwx_import_profile/
RUN PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app PYTHON_CPU_COUNT=2 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
    python /tmp/rwx_import_profile/build_import_blocks.py /opt/sqlglot coverage.json.gz import-blocks.json.gz generators.json.gz --build-budget 330 \
    && chmod 644 import-blocks.json.gz generators.json.gz

ENV PYTHONDONTWRITEBYTECODE=1 SQLGLOT_ROOT=/opt/sqlglot
USER 65534:65534
ENTRYPOINT ["python", "selector.py"]
