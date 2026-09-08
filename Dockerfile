# Build stage
FROM rust:1.86-slim AS builder
WORKDIR /app

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
