#!/usr/bin/env bash
set -Eeuo pipefail

STEP=初始化
trap 'code=$?; echo "Docker 安装失败（步骤：$STEP，退出码：$code）" >&2; exit "$code"' ERR

if [ "$(id -u)" -eq 0 ]; then
  SUDO=
elif command -v sudo >/dev/null 2>&1 && sudo --non-interactive true; then
  SUDO='sudo --non-interactive'
else
  echo '安装 Docker 需要 root 或免密 sudo' >&2
  exit 1
fi

as_root() {
  if [ -n "$SUDO" ]; then
    $SUDO "$@"
  else
    "$@"
  fi
}

apt_get() {
  as_root env DEBIAN_FRONTEND=noninteractive \
    apt-get -o Dpkg::Use-Pty=0 "$@"
}

configure_registry_mirrors() {
  STEP=配置DockerHub镜像加速
  echo '[Docker] 合并 Docker Hub 镜像加速配置'
  as_root install -d -m 0755 /etc/docker
  as_root python3 - /etc/docker/daemon.json <<'PY'
import json
import os
import sys
import tempfile

path = sys.argv[1]
mirrors = [
    "https://docker.1ms.run",
    "https://docker.m.daocloud.io",
    "https://dockerproxy.link",
    "https://free.hubfast.cn",
    "https://proxy.vvvv.ee",
    "https://docker.jiaxin.site",
    "https://docker.xuanyuan.me",
    "https://registry.cyou",
    "https://cf-workers-docker-io-apl.pages.dev",
]
try:
    with open(path, encoding="utf-8") as source:
        config = json.load(source)
except FileNotFoundError:
    config = {}
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"无法安全合并 {path}：{exc}")
if not isinstance(config, dict):
    raise SystemExit(f"{path} 顶层必须是 JSON 对象")
existing = config.get("registry-mirrors", [])
if not isinstance(existing, list) or not all(isinstance(item, str) for item in existing):
    raise SystemExit(f"{path} 的 registry-mirrors 必须是字符串数组")
merged = list(dict.fromkeys([*existing, *mirrors]))
changed = merged != existing
config["registry-mirrors"] = merged
if "max-concurrent-downloads" not in config:
    config["max-concurrent-downloads"] = 10
    changed = True
if "max-concurrent-uploads" not in config:
    config["max-concurrent-uploads"] = 5
    changed = True
if changed:
    descriptor, temporary = tempfile.mkstemp(prefix="daemon.json.mcsp-", dir="/etc/docker")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(config, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
print("changed" if changed else "unchanged")
PY
}

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  STEP=验证现有安装
  echo '[Docker 1/2] 验证现有 Engine 与 Compose'
  as_root systemctl enable --now docker.service containerd.service
  if [ "$(id -un)" != root ]; then
    as_root usermod --append --groups docker "$(id -un)"
  fi
  mirror_state=$(configure_registry_mirrors | tee /dev/stderr | tail -n 1)
  if [ "$mirror_state" = changed ]; then
    as_root systemctl restart docker.service
  fi
  echo '[Docker 2/2] 验证镜像加速后的 Docker Engine'
  as_root docker info >/dev/null
  echo 'Docker Engine 与 Compose 插件已存在并验证'
  exit 0
fi

test -r /etc/os-release || { echo '无法读取 /etc/os-release' >&2; exit 1; }
. /etc/os-release
case "${ID:-}" in
  debian|ubuntu) distribution=$ID ;;
  *)
    echo '尚不支持在该发行版上自动安装 Docker；请手工安装 Engine 与 Compose 插件' >&2
    exit 1
    ;;
esac
codename=${VERSION_CODENAME:-}
case "$codename" in
  ''|*[!a-zA-Z0-9.-]*) echo '/etc/os-release 缺少安全的 VERSION_CODENAME' >&2; exit 1 ;;
esac
architecture=$(dpkg --print-architecture)
case "$architecture" in
  ''|*[!a-zA-Z0-9]*) echo '无法识别 dpkg 架构' >&2; exit 1 ;;
esac

STEP=准备下载工具
echo '[Docker 1/6] 准备 ca-certificates 与 curl'
apt_get update
apt_get install -y ca-certificates curl
conflicts=
for package in docker.io docker-compose docker-doc docker-buildx podman-docker containerd runc; do
  if dpkg-query -W -f='${db:Status-Abbrev}' "$package" 2>/dev/null | grep -q '^ii'; then
    conflicts="$conflicts $package"
  fi
done
if [ -n "$conflicts" ]; then
  # shellcheck disable=SC2086
  apt_get remove -y $conflicts
fi

STEP=下载仓库密钥
echo '[Docker 2/6] 下载 Docker 官方仓库密钥'
as_root install -m 0755 -d /etc/apt/keyrings
key_url="https://download.docker.com/linux/$distribution/gpg"
if ! as_root curl --fail --show-error --location \
  --retry 5 --retry-delay 2 --retry-all-errors \
  --connect-timeout 15 --max-time 180 \
  "$key_url" -o /etc/apt/keyrings/docker.asc; then
  echo "无法下载 Docker 仓库密钥：$key_url" >&2
  echo '请检查目标服务器访问 download.docker.com 的网络后重试' >&2
  exit 35
fi
as_root chmod a+r /etc/apt/keyrings/docker.asc
source_text=$(printf '%s\n' \
  'Types: deb' \
  "URIs: https://download.docker.com/linux/$distribution" \
  "Suites: $codename" \
  'Components: stable' \
  "Architectures: $architecture" \
  'Signed-By: /etc/apt/keyrings/docker.asc')
if [ -n "$SUDO" ]; then
  printf '%s\n' "$source_text" | $SUDO tee /etc/apt/sources.list.d/docker.sources >/dev/null
else
  printf '%s\n' "$source_text" > /etc/apt/sources.list.d/docker.sources
fi

STEP=更新Docker仓库
echo '[Docker 3/6] 更新 Docker 软件仓库'
apt_get update
STEP=安装Docker
echo '[Docker 4/6] 安装 Engine、Buildx 与 Compose'
apt_get install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

STEP=启动并验证Docker
echo '[Docker 5/6] 配置 Docker Hub 镜像加速'
configure_registry_mirrors
STEP=启动并验证Docker
echo '[Docker 6/6] 启动并验证 Docker'
as_root systemctl enable --now docker.service containerd.service
if [ "$(id -un)" != root ]; then
  as_root usermod --append --groups docker "$(id -un)"
fi
as_root docker info >/dev/null
docker compose version >/dev/null
echo 'Docker Engine 与 Compose 插件安装完成'
if [ "$(id -un)" != root ]; then
  echo "已将 $(id -un) 加入 docker 组；重新登录后生效"
fi
