# syntax=docker/dockerfile:1
#
# Dev image for the solar API. Two things it deliberately does NOT do:
#
#   * no multi-stage build -- this is a local development stack, and a slim
#     runtime image would complicate the edit loop for no benefit here;
#   * no non-root user -- the container bind-mounts ./api/src and reads
#     secrets/, and getting uid alignment right across macOS bind mounts costs
#     more than it buys. Do not reuse this image anywhere real.
#
# For production this would be a multi-stage build on a distroless base, with
# dependencies pinned and no source on the filesystem.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Keep the venv inside /app so a bind mount of ./src cannot shadow it.
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Dependencies first, so editing source does not invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-install-project

COPY src ./src
RUN uv sync

EXPOSE 8000

# Called directly rather than through `uv run`, which would re-resolve the
# lockfile on every start and add seconds to each reload.
CMD ["/app/.venv/bin/uvicorn", "solar_api.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--reload"]
