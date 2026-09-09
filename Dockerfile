# Build stage (latest stable — some transitive deps require rustc >= 1.88)
# Pin both stages to the SAME Debian release: a binary built on trixie
# (glibc 2.39) cannot run on bookworm (glibc 2.36) — the panel container
# crashed on startup with "GLIBC_2.38 not found".
FROM rust:1-slim-bookworm AS builder
WORKDIR /app

# Commit the image was built from, passed as --build-arg by install.sh /
# update.sh. The binary reads it at compile time (option_env!) and serves it
# as the running version in /api/update — without it the panel always thinks
# it is up to date ("unknown" == latest).
ARG UPDATE_COMMIT=unknown
ENV UPDATE_COMMIT=${UPDATE_COMMIT}

# Cache dependencies: build with a dummy main first
COPY Cargo.toml Cargo.lock ./
RUN mkdir -p src && echo 'fn main() {}' > src/main.rs \
    && cargo build --release \
    && rm -rf src target/release/deps/forgefoxvpn_provider* target/release/forgefoxvpn-provider

# Build the real binary (migrations are embedded into the binary by sqlx::migrate!)
COPY src ./src
COPY migrations ./migrations
RUN touch src/main.rs && cargo build --release

# Runtime stage
FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/target/release/forgefoxvpn-provider /app/forgefoxvpn-provider
COPY public ./public

EXPOSE 8080
CMD ["/app/forgefoxvpn-provider"]
