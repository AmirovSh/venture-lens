#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get install -y docker.io
systemctl enable --now docker
cd /opt/venture-lens/source
api_tag=$(python3 -c "import yaml; d=yaml.safe_load(open('deploy/overlays/demo/kustomization.yaml')); print(next(i['newTag'] for i in d['images'] if i['name']=='venture-lens-api'))")
web_tag=$(python3 -c "import yaml; d=yaml.safe_load(open('deploy/overlays/demo/kustomization.yaml')); print(next(i['newTag'] for i in d['images'] if i['name']=='venture-lens-web'))")
docker build --build-arg PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.11-slim -f Dockerfile.api -t "venture-lens-api:$api_tag" .
docker build --build-arg NODE_IMAGE=public.ecr.aws/docker/library/node:22-alpine -f Dockerfile.web -t "venture-lens-web:$web_tag" .
vpn_tag=$(python3 -c "import yaml; d=yaml.safe_load(open('deploy/overlays/demo/kustomization.yaml')); print(next(i['newTag'] for i in d['images'] if i['name']=='venture-lens-vpn-gateway'))")
docker build -f platform/server/Dockerfile.vpn-gateway -t "venture-lens-vpn-gateway:$vpn_tag" platform/server
docker pull postgres:17.12-alpine
docker pull chrislusf/seaweedfs:4.54
docker pull caddy@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0
docker save "venture-lens-api:$api_tag" "venture-lens-web:$web_tag" "venture-lens-vpn-gateway:$vpn_tag" postgres:17.12-alpine chrislusf/seaweedfs:4.54 caddy@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0 -o /opt/venture-lens/images.tar
ctr -n k8s.io images import --platform linux/amd64 /opt/venture-lens/images.tar
rm /opt/venture-lens/images.tar
echo BUILD_COMPLETE
