#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get install -y docker.io
systemctl enable --now docker
cd /opt/venture-lens/source
docker build --build-arg PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.11-slim -f Dockerfile.api -t venture-lens-api:demo-bootstrap .
docker build --build-arg NODE_IMAGE=public.ecr.aws/docker/library/node:22-alpine -f Dockerfile.web -t venture-lens-web:demo-bootstrap .
docker pull postgres:17.12-alpine
docker pull chrislusf/seaweedfs:4.54
docker save venture-lens-api:demo-bootstrap venture-lens-web:demo-bootstrap postgres:17.12-alpine chrislusf/seaweedfs:4.54 -o /opt/venture-lens/images.tar
ctr -n k8s.io images import --platform linux/amd64 /opt/venture-lens/images.tar
rm /opt/venture-lens/images.tar
echo BUILD_COMPLETE
