# CI image for .github/workflows/validate-pr.yml — Python + tabix/bgzip + NCBI datasets + reviewdog.
# Published to GHCR by publish-ci-validator.yml
FROM python:3.11-slim-bookworm

ARG DATASETS_RELEASE=v18.26.0
ARG REVIEWDOG_SEMVER=0.20.3

LABEL org.opencontainers.image.title="annotrieve-registry-ci"
LABEL org.opencontainers.image.description="PR validation: validate_pr.py + reviewdog"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        tabix \
        unzip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# NCBI datasets CLI (pinned GitHub release asset; includes linux-amd64 binary)
RUN curl -fsSL \
        "https://github.com/ncbi/datasets/releases/download/${DATASETS_RELEASE}/linux-amd64.cli.package.zip" \
        -o /tmp/datasets-cli.zip \
    && unzip -q /tmp/datasets-cli.zip -d /tmp/datasets-cli \
    && DATASETS_BIN="$(find /tmp/datasets-cli -name datasets -type f | head -1)" \
    && test -n "$DATASETS_BIN" \
    && install -m0755 "$DATASETS_BIN" /usr/local/bin/datasets \
    && rm -rf /tmp/datasets-cli.zip /tmp/datasets-cli \
    && datasets --version

# reviewdog (pinned)
RUN curl -fsSL \
        "https://github.com/reviewdog/reviewdog/releases/download/v${REVIEWDOG_SEMVER}/reviewdog_${REVIEWDOG_SEMVER}_Linux_x86_64.tar.gz" \
        -o /tmp/reviewdog.tgz \
    && mkdir -p /tmp/reviewdog-extract \
    && tar -xzf /tmp/reviewdog.tgz -C /tmp/reviewdog-extract \
    && RD="$(find /tmp/reviewdog-extract -name reviewdog -type f | head -1)" \
    && test -n "$RD" \
    && install -m0755 "$RD" /usr/local/bin/reviewdog \
    && rm -rf /tmp/reviewdog.tgz /tmp/reviewdog-extract \
    && reviewdog -version

ENV DATASETS_BINARY=/usr/local/bin/datasets

WORKDIR /workspace
