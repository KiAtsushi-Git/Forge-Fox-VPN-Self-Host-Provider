# ForgeFox VPN Provider — Python edition.
# No compilation stage: pip install + copy, so the image builds in seconds
# (the Rust build took minutes compiling the TUN bridge deps).
FROM python:3.12-slim-bookworm

# Commit the image was built from, passed as --build-arg by install.sh /
# update.sh. The app reads it at runtime and serves it as the running
# version in /api/update.
ARG UPDATE_COMMIT=unknown
ENV UPDATE_COMMIT=${UPDATE_COMMIT} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# git + docker-cli: the self-update script (update.sh) runs INSIDE this
# container (spawned by /api/update/run) and needs git to clone the repo and
# docker to rebuild the image through the mounted host socket.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY public ./public

EXPOSE 8080
CMD ["python", "-m", "app.main"]
