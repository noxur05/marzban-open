ARG PYTHON_VERSION=3.12

FROM python:$PYTHON_VERSION-slim AS build

ENV PYTHONUNBUFFERED=1

WORKDIR /code

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl unzip gcc python3-dev libpq-dev \
    && curl -L https://github.com/Gozargah/Marzban-scripts/raw/master/install_latest_xray.sh | bash \
    && rm -rf /var/lib/apt/lists/*

COPY ./requirements.txt /code/
RUN python3 -m pip install --upgrade pip "setuptools<70" \
    && pip install --no-cache-dir --upgrade -r /code/requirements.txt

# --- Rust builder: compile the sidecars into this same image (one image, per
# custom-build). Bookworm keeps the build glibc == the python:3.12-slim
# (bookworm) runtime below. Three binaries come out of one crate:
#   marzban-stats-collector  hourly per-admin usage snapshots
#   marzban-subs-poller      mirrors other panels' subscription hosts to Redis
#   marzban-google-sync      keeps Micky users' encrypted Google copies current
# ---
FROM rust:1-slim-bookworm AS rust-build
WORKDIR /collector
COPY stats-collector/Cargo.toml stats-collector/Cargo.lock ./
# Dependency-cache layer: stub out every target declared in Cargo.toml so the
# dependency graph compiles without the real sources. An empty lib.rs is enough
# — cargo builds all declared dependencies regardless of whether the stubs
# reference them — and every [[bin]] path must exist or cargo errors out.
RUN mkdir -p src/bin \
    && : > src/lib.rs \
    && echo 'fn main() {}' > src/main.rs \
    && echo 'fn main() {}' > src/bin/subs-poller.rs \
    && echo 'fn main() {}' > src/bin/google-sync.rs \
    && cargo build --release --locked && rm -rf src
COPY stats-collector/src ./src
RUN touch src/lib.rs src/main.rs src/bin/subs-poller.rs src/bin/google-sync.rs \
    && cargo build --release --locked \
    && strip target/release/marzban-stats-collector target/release/marzban-subs-poller \
        target/release/marzban-google-sync

FROM python:$PYTHON_VERSION-slim

ENV PYTHON_LIB_PATH=/usr/local/lib/python${PYTHON_VERSION%.*}/site-packages
WORKDIR /code

RUN rm -rf $PYTHON_LIB_PATH/*

COPY --from=build $PYTHON_LIB_PATH $PYTHON_LIB_PATH
COPY --from=build /usr/local/bin /usr/local/bin
COPY --from=build /usr/local/share/xray /usr/local/share/xray
# Bundled sidecars. Each runs as a SEPARATE container off this same image via a
# `command:` override; the panel itself never invokes any of them. Separate
# processes on purpose — the release profile sets panic = "abort", so sharing one
# would let a panic in one sidecar take the others down with it.
COPY --from=rust-build /collector/target/release/marzban-stats-collector /usr/local/bin/marzban-stats-collector
COPY --from=rust-build /collector/target/release/marzban-subs-poller /usr/local/bin/marzban-subs-poller
COPY --from=rust-build /collector/target/release/marzban-google-sync /usr/local/bin/marzban-google-sync

COPY . /code

RUN ln -s /code/marzban-cli.py /usr/bin/marzban-cli \
    && chmod +x /usr/bin/marzban-cli \
    && marzban-cli completion install --shell bash

CMD ["bash", "-c", "alembic upgrade head; python main.py"]
