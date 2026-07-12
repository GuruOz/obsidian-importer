FROM node:22-slim

# node:22-slim already bundles tzdata; set the default zone to match the pipeline
# so the bridge buckets messages by Singapore calendar day (see index.js).
ENV TZ=Asia/Singapore

# Baileys pulls a transitive dependency (libsignal) straight from GitHub, so npm
# needs git + certs at build time; node:22-slim ships without them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json ./
# No package-lock committed; npm install resolves the pinned versions in package.json.
RUN npm install --omit=dev && npm cache clean --force

COPY index.js ./

CMD ["node", "index.js"]
