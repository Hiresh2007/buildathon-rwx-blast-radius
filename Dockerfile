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

COPY tools/profile_cold_test.py tools/pack_cold_coverage.py tools/build_cold_profiles.py /tmp/cold-tools/
RUN python /tmp/cold-tools/build_cold_profiles.py /opt/sqlglot coverage.json.gz \
    tests/test_executor.py::TestExecutor::test_execute_tpcds tests/test_optimizer.py

ENV PYTHONDONTWRITEBYTECODE=1 SQLGLOT_ROOT=/opt/sqlglot
USER 65534:65534
ENTRYPOINT ["python", "selector.py"]
