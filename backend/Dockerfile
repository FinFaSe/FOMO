# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=system_ca \
    if [ -f /run/secrets/system_ca ]; then export SSL_CERT_FILE=/run/secrets/system_ca PIP_CERT=/run/secrets/system_ca; fi; \
    pip install --no-cache-dir uv==0.12.23 && uv sync --frozen --no-dev
COPY tracker ./tracker
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "tracker.main:app", "--host", "0.0.0.0", "--port", "8000"]
