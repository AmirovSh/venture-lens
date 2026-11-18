#!/usr/bin/env bash
# Fresh Ubuntu VPS only. Run after a second key-authenticated session was verified.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
test "$(id -u)" = 0
. /etc/os-release
test "$ID" = ubuntu
test "$VERSION_ID" = 24.04
install -d -m 700 /opt/venture-lens /var/log/venture-lens
apt-get update
apt-get install -y ca-certificates curl ufw fail2ban unattended-upgrades python3 git jq
cat >/etc/ssh/sshd_config.d/00-venture-lens.conf <<'SSH'
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
MaxAuthTries 3
X11Forwarding no
SSH
sshd -t
systemctl reload ssh
ufw allow 22/tcp comment 'Venture Lens SSH'
ufw allow from 10.42.0.0/16 to any comment 'K3s pod network'
ufw allow from 10.43.0.0/16 to any comment 'K3s service network'
ufw default deny incoming
ufw default allow outgoing
ufw --force enable
systemctl enable --now fail2ban
# Keep Kubernetes administration private. Web ingress will be enabled after auth/TLS.
install -d -m 700 /etc/rancher/k3s
cat >/etc/rancher/k3s/config.yaml <<'K3S'
write-kubeconfig-mode: "0600"
disable:
  - traefik
  - servicelb
secrets-encryption: true
K3S
version='v1.37.2+k3s1'
base="https://github.com/k3s-io/k3s/releases/download/${version}"
work=$(mktemp -d)
trap 'rm -f "$work/k3s" "$work/sha256sum-amd64.txt" "$work/install.sh"; rmdir "$work"' EXIT
curl --fail --location --retry 3 "$base/sha256sum-amd64.txt" -o "$work/sha256sum-amd64.txt"
curl --fail --location --retry 3 "$base/k3s" -o "$work/k3s"
(cd "$work"; awk '$2 == "k3s" {print}' sha256sum-amd64.txt | sha256sum -c -)
install -m 755 "$work/k3s" /usr/local/bin/k3s
# Installer is tied to the same release tag; it registers systemd without downloading a binary.
curl --fail --location --retry 3 "https://raw.githubusercontent.com/k3s-io/k3s/${version}/install.sh" -o "$work/install.sh"
INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_VERSION="$version" sh "$work/install.sh" server
timeout 180 bash -c 'until k3s kubectl get nodes >/dev/null 2>&1; do sleep 3; done'
k3s kubectl wait --for=condition=Ready node --all --timeout=180s
date -u +%FT%TZ >/opt/venture-lens/bootstrap.complete
echo 'BOOTSTRAP_COMPLETE'
