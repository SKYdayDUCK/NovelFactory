#!/usr/bin/env bash
# Install the requested engineering tools from the signed Ubuntu and PostgreSQL archives.
set -euo pipefail
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_CODENAME" == noble && "$#" -gt 0 ]]
for package in "$@"; do
  [[ "$package" =~ ^[a-z0-9][a-z0-9.+-]+$ ]]
done
installed=1
for package in "$@"; do
  if [[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)" != "install ok installed" ]]; then
    installed=0
  fi
done
if (( installed )); then
  printf 'All requested packages are already installed.\n'
  exit 0
fi
archive=$(mktemp -d /tmp/nf-ci-apt.XXXXXXXX)
trap 'sudo rm -rf -- "$archive"' EXIT
chmod 0755 "$archive"
sudo install -d -m 0755 "$archive/lists" "$archive/lists/partial"
sudo chown _apt:root "$archive/lists/partial"
key="$archive/pgdg.asc"
curl -fsSL --retry 3 --connect-timeout 10 --max-time 60 https://www.postgresql.org/media/keys/ACCC4CF8.asc -o "$key"
gpg --batch --show-keys --with-colons "$key" | awk -F: '$1 == "fpr" {print $10}' | grep -Fx B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8
ubuntu=/usr/share/keyrings/ubuntu-archive-keyring.gpg
test -r "$ubuntu"
sources="$archive/sources.list"
options=(
  -o "Dir::Etc::sourcelist=$sources" -o Dir::Etc::sourceparts=-
  -o "Dir::State::lists=$archive/lists"
  -o Acquire::Languages=none -o Acquire::ForceIPv4=true
  -o Acquire::Retries=3 -o Acquire::https::Timeout=25
  -o Acquire::IndexTargets::deb::DEP-11::DefaultEnabled=false
  -o APT::Update::Error-Mode=any
)
# A byte-range curl probe can succeed even when APT cannot fetch the complete
# signed index. Retry the actual index and package operations on another mirror.
for mirror in https://azure.archive.ubuntu.com/ubuntu https://archive.ubuntu.com/ubuntu https://mirrors.edge.kernel.org/ubuntu; do
  : > "$sources"
  for suite in noble noble-updates noble-security; do
    printf 'deb [signed-by=%s] %s %s main universe\n' "$ubuntu" "$mirror" "$suite" >> "$sources"
  done
  printf 'deb [signed-by=%s] https://apt.postgresql.org/pub/repos/apt noble-pgdg main\n' "$key" >> "$sources"
  if sudo apt-get "${options[@]}" -o Acquire::https::Timeout=12 update -qq &&
     sudo apt-get "${options[@]}" -o Acquire::https::Timeout=12 install -y "$@"; then
    exit 0
  fi
  echo "Signed package installation failed via $mirror; trying next archive" >&2
done
exit 1
