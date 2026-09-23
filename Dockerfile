# Runs the platform, and the code it generates, inside a container.
#
# Generated code is executed by the validation stage. On a host that means
# untrusted code running as you; here it runs as a non-root user in a container
# with no credentials and nothing mounted.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY asep ./asep
RUN pip install --no-cache-dir -e ".[dev]"

COPY sample_codebase ./sample_codebase
COPY scripts ./scripts
COPY tests ./tests

RUN useradd --create-home --uid 10001 asep \
    && mkdir -p /app/runs \
    && chown -R asep:asep /app
USER asep

ENTRYPOINT ["python", "-m", "asep"]
CMD ["url_shortener"]
