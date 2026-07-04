#!/usr/bin/env bash
set -e

echo "Pulling latest changes from git..."
git pull

echo "Rebuilding pipeline Docker image..."
docker compose build pipeline

echo "Restarting containers..."
docker compose up -d

echo "Upgrade complete!"
