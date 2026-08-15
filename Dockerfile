FROM ghcr.io/astral-sh/uv:0.11.14-python3.13-bookworm-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git gh \
    && apt-get clean

WORKDIR /opt/maida-heal
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/maida-heal/.venv/bin:${PATH}"

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY maida ./maida
RUN uv sync --frozen --no-dev

WORKDIR /work
ENTRYPOINT ["/opt/maida-heal/.venv/bin/maida-heal"]
CMD ["watch", "--interval", "300"]
