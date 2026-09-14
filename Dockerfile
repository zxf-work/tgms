# syntax=docker/dockerfile:1
#
# Multi-stage build: the builder installs the pinned Rust toolchain (must
# track rust-toolchain.toml — bump both together) plus uv, and builds the
# wheel via the project's real build backend (maturin; see pyproject.toml
# [build-system] / [tool.maturin] — the package is a mixed Rust/Python
# build, D-028/D-029). The runtime stage only ever sees the built wheel: no
# Rust toolchain, no source tree, no build-time dependencies ship in the
# final image.

FROM python:3.12-slim AS builder

# Must match rust-toolchain.toml's `channel`. Not read automatically here
# (rustup takes an explicit --default-toolchain); keep the two in sync by
# hand, same as the CI step in .github/workflows/ci.yml.
ARG RUST_VERSION=1.98.1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
        sh -s -- -y --profile minimal --default-toolchain "${RUST_VERSION}" --component clippy \
    && rustc --version && cargo --version

RUN pip install --no-cache-dir uv

WORKDIR /src
COPY . .

# `uv build` drives the PEP 517 backend declared in pyproject.toml
# (maturin), which in turn invokes cargo against the pinned toolchain
# above. Output: a single wheel under /wheels, named with pip's required
# {name}-{version}-{tags}.whl shape (so it cannot simply be renamed).
RUN uv build --wheel --out-dir /wheels

FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.source="https://github.com/zxf-work/tgms" \
      org.opencontainers.image.description="Agent-Native Temporal Graph Management System"

RUN groupadd --system tgms && useradd --system --gid tgms --home-dir /data --create-home tgms

COPY --from=builder /wheels /wheels
# `agent` extra: the litellm/fastmcp surface used by `tgms call` / the MCP
# server; the base install (demo, ask, ingest, replay, ...) needs neither.
# Resolved via `ls` rather than a bare glob: the real wheel filename
# carries an interpreter/platform tag (e.g. cp312-cp312-linux_aarch64),
# and pip's extras syntax appended directly to `*.whl[agent]` doesn't
# glob-expand — `[agent]` is itself a shell glob character class.
RUN pip install --no-cache-dir "$(ls /wheels/*.whl)[agent]" && rm -rf /wheels

# The event log and derived store are the only state that must survive a
# container recreation (docs/STABILITY.md §1: event logs are stable, stores
# are a rebuildable cache) — both live under one store directory.
VOLUME ["/data"]
WORKDIR /data
USER tgms

ENTRYPOINT ["tgms"]
CMD ["demo"]
