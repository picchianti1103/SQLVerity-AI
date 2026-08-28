FROM python:3.14-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5 AS builder

ARG SQLVERITY_EXTRAS=postgres,openai,identity,secrets,observability

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src

COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY apps ./apps
COPY migrations ./migrations
COPY packages ./packages

RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels ".[${SQLVERITY_EXTRAS}]"

FROM python:3.14-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5 AS runtime

ARG SQLVERITY_EXTRAS=postgres,openai,identity,secrets,observability

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY --from=builder /wheels /wheels

RUN apt-get update \
    && apt-get install --yes --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --no-index --find-links=/wheels \
        "sqlverity-platform[${SQLVERITY_EXTRAS}]" \
    && rm -rf /wheels \
    && addgroup --system sqlverity \
    && adduser --system --ingroup sqlverity --home /app sqlverity \
    && mkdir -p /data \
    && chown -R sqlverity:sqlverity /app /data

USER sqlverity

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
