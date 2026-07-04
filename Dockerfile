FROM python:3.12-slim

ARG SUPERCRONIC_VERSION=v0.2.33
ARG SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64
ARG SUPERCRONIC_SHA1SUM=71b0d58cc53f6bd72cf2f293e09e294b79c666d8

RUN apt-get update && apt-get install -y --no-install-recommends \
        rsync \
        curl \
        ca-certificates \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "${SUPERCRONIC_URL}" -o /usr/local/bin/supercronic \
    && echo "${SUPERCRONIC_SHA1SUM}  /usr/local/bin/supercronic" | sha1sum -c - \
    && chmod +x /usr/local/bin/supercronic

WORKDIR /app

COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY scripts/ ./scripts/
COPY prompt_template.txt prompt_dry_run.txt prompt_vault_profile.txt prompt_weekly_rollup.txt ./
COPY crontab ./crontab

RUN chmod +x scripts/*.sh

ENV PYTHONUNBUFFERED=1

CMD ["supercronic", "-no-reap", "-passthrough-logs", "/app/crontab"]
