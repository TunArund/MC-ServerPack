"""Deployment and remote-operation service used by every local frontend."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

from . import config
from .modpack import validate_id
from .remote import CommandError, archive_tree, executable, run, scp, ssh

ROOT = config.ROOT
MANAGER = ROOT / "manager"
GATEWAY = ROOT / "qqbot"
ENV_FILE = ROOT / ".env"

BOOTSTRAP = r"""set -Eeuo pipefail
need=0
command -v docker >/dev/null 2>&1 || need=1
docker compose version >/dev/null 2>&1 || need=1
if [ "$REQUIRE_PYTHON" = 1 ]; then
  command -v python3 >/dev/null 2>&1 || need=1
  python3 -c 'import sys, yaml; raise SystemExit(sys.version_info < (3, 8))' >/dev/null 2>&1 || need=1
  command -v crontab >/dev/null 2>&1 || need=1
fi
if [ "$(id -u)" -eq 0 ]; then run_root() { "$@"; }
else run_root() { sudo -n "$@"; }; fi
if [ "$need" = 1 ]; then
  [ "$(id -u)" -eq 0 ] || sudo -n true >/dev/null 2>&1 || { echo '安装运行环境需要免密 sudo' >&2; exit 1; }
  . /etc/os-release
  case "$ID" in debian|ubuntu) ;; *) echo "暂不支持的系统: $ID" >&2; exit 1 ;; esac
  run_root apt-get update
  packages=(ca-certificates curl gnupg)
  if [ "$REQUIRE_PYTHON" = 1 ]; then packages+=(python3 python3-yaml cron); fi
  run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
  if ! docker compose version >/dev/null 2>&1; then
    run_root install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /tmp/docker.asc
    run_root install -m 0644 /tmp/docker.asc /etc/apt/keyrings/docker.asc
    rm -f /tmp/docker.asc
    arch="$(dpkg --print-architecture)"
    repo="deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable"
    run_root sh -c "printf '%s\\n' '$repo' > /etc/apt/sources.list.d/docker.list"
    run_root apt-get update
    run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  run_root systemctl enable --now docker
  if [ "$REQUIRE_PYTHON" = 1 ]; then run_root systemctl enable --now cron; fi
fi
if [ "$(id -u)" -ne 0 ] && ! id -nG | tr ' ' '\n' | grep -qx docker; then
  sudo -n true >/dev/null 2>&1 || { echo '加入 docker 组需要免密 sudo' >&2; exit 1; }
  run_root usermod -aG docker "$(id -un)"
  echo '当前用户已加入 docker 组；请重新登录后重试（不会使用 sudo 运行 Docker）' >&2
  exit 3
fi
docker info >/dev/null 2>&1 || {
  echo '当前会话无权访问 Docker；请重新登录或执行 newgrp docker 后重试' >&2
  exit 3
}
if [ "$REQUIRE_PYTHON" = 1 ]; then
  python3 -c 'import sys, yaml; raise SystemExit(sys.version_info < (3, 8))' \
    || { echo 'manager 需要 Python 3.8+ 和 PyYAML' >&2; exit 1; }
fi
"""


def load_env() -> dict[str, str]:
    if not ENV_FILE.is_file():
        raise RuntimeError(f"缺少 {ENV_FILE}，请从 .env.example 复制并填写")
    values: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


class Controller:
    def __init__(self) -> None:
        self.doc = config.load()

    def section(self, name: str) -> dict:
        value = self.doc.get(name)
        if not isinstance(value, dict) or not value:
            raise RuntimeError(f"config.yml 未配置 {name}")
        return value

    def target(self, name: str) -> str:
        item = self.section(name)
        return f"{item['user']}@{item['host']}"

    def directory(self, name: str) -> str:
        return str(self.section(name)["dir"])

    def remote_manager(self, args: list[str]) -> None:
        directory = self.directory("manager")
        ssh(self.target("manager"), ["bash", "-lc", f"cd {shellq(directory)} && exec ./manager.sh {shelljoin(args)}"])

    def install_dependencies(self, target: str, log: str) -> None:
        if target == "manager":
            print("==> [manager] 检查 Python 环境")
        print(f"==> [{target}] 检查 Docker 环境")
        require = "1" if target == "manager" else "0"
        script = f"exec >>{shellq(log)} 2>&1\n{BOOTSTRAP}"
        try:
            ssh(self.target(target), ["env", f"REQUIRE_PYTHON={require}", "bash", "-s"], input_text=script)
        except CommandError as exc:
            raise RuntimeError(f"环境检查失败；详情见 {self.target(target)}:{log}") from exc

    def deploy_manager(self) -> None:
        directory, target = self.directory("manager"), self.target("manager")
        log = directory + "/deploy.log"
        if not (MANAGER / "manager.sh").is_file():
            raise RuntimeError("缺少 manager/manager.sh")
        ssh(target, ["mkdir", "-p", directory])
        ssh(target, ["bash", "-lc", f": > {shellq(log)}"])
        self.install_dependencies("manager", log)
        print(f"==> [manager] 同步到 {target}:{directory}")
        archive = archive_tree(MANAGER, exclude={"modpacks", "config.yml"})
        try:
            scp(archive, f"{target}:{directory}/.manager-upload.tar.gz")
            script = (
                f"rm -rf {shellq(directory + '/lib')} {shellq(directory + '/runtime')}; "
                f"rm -f {shellq(directory + '/manager.sh')} {shellq(directory + '/.dockerignore')}; "
                f"tar -xzf {shellq(directory + '/.manager-upload.tar.gz')} -C {shellq(directory)}; "
                f"rm -f {shellq(directory + '/.manager-upload.tar.gz')}"
            )
            ssh(target, ["bash", "-lc", script])
        finally:
            shutil.rmtree(archive.parent, ignore_errors=True)
        runtime = {"manager": {"modpack": self.doc["manager"].get("modpack", {})}}
        with tempfile.TemporaryDirectory(prefix="mc-serverpack-") as temp:
            output = Path(temp) / "config.yml"
            output.write_text(yaml.safe_dump(runtime, sort_keys=False, allow_unicode=True), encoding="utf-8")
            scp(output, f"{target}:{directory}/config.yml")
        versions = self.doc["manager"].get("java", [21])
        print(f"==> [manager] Java 环境（可用 8 17 21）：{' '.join(map(str, versions))}")
        print(f"==> [manager] 构建 Java 镜像（详情：{target}:{log}）")
        build = " && ".join(
            f"docker build -f runtime/java{version}.dockerfile -t mc-java:{version} ."
            for version in versions
        )
        try:
            ssh(target, ["bash", "-lc", f"cd {shellq(directory)} && ( {build} ) >> {shellq(log)} 2>&1"])
        except CommandError as exc:
            raise RuntimeError(f"Java 镜像构建失败；详情见 {target}:{log}") from exc
        print("manager 部署完成；使用 ./mc.sh modpack deploy <ID> 上传整合包")

    def deploy_modpack(self, pack: str) -> None:
        validate_id(pack)
        if pack == "template":
            raise RuntimeError("template 仅用于创建整合包，不能部署")
        local = MANAGER / "modpacks" / pack
        if not (local / "src").is_dir() or not (local / "config.yml").is_file():
            raise RuntimeError(f"本地整合包不存在：{local}")
        run([sys.executable, str(MANAGER / "lib/config.py"), "validate", pack])
        base, target = self.directory("manager"), self.target("manager")
        remote = f"{base}/modpacks/{pack}"
        check = f"test -x {shellq(base + '/manager.sh')} && mkdir -p {shellq(remote + '/src')} {shellq(remote + '/data')} {shellq(remote + '/backups')}"
        try:
            ssh(target, ["bash", "-lc", check])
        except CommandError as exc:
            raise RuntimeError("远端 manager 尚未部署，请先运行 ./mc.sh deploy manager") from exc
        print(f"==> [modpack] 上传 {pack} 到 {target}:{remote}")
        self._upload_directory(local / "src", target, remote + "/src", replace=True)
        scp(local / "config.yml", f"{target}:{remote}/config.yml")
        self._upload_directory(local / "data", target, remote + "/data", replace=False)
        self.remote_manager(["config", pack])
        print(f"整合包已部署：{pack}")

    @staticmethod
    def _upload_directory(local: Path, target: str, remote: str, *, replace: bool) -> None:
        archive = archive_tree(local)
        remote_archive = remote + ".upload.tar.gz"
        try:
            scp(archive, f"{target}:{remote_archive}")
            if replace:
                prepare = f"rm -rf {shellq(remote)}; mkdir -p {shellq(remote)}"
                extract = f"tar -xzf {shellq(remote_archive)} -C {shellq(remote)}"
            else:
                prepare = f"mkdir -p {shellq(remote)}"
                extract = f"tar --skip-old-files -xzf {shellq(remote_archive)} -C {shellq(remote)}"
            ssh(target, ["bash", "-lc", f"{prepare}; {extract}; rm -f {shellq(remote_archive)}"])
        finally:
            shutil.rmtree(archive.parent, ignore_errors=True)

    def gateway_env(self) -> dict[str, str]:
        env = load_env()
        for key in ("GW_SECRET_ID", "GW_SECRET_KEY", "QQBOT_SECRET_ID", "QQBOT_SECRET_KEY"):
            if not env.get(key):
                raise RuntimeError(f".env 缺少 {key}")
        pack = config.choose_pack()
        result = run([sys.executable, str(MANAGER / "lib/config.py"), "pack", pack, "port"], capture=True)
        gateway, schedule = self.section("gateway"), self.section("gateway")["schedule"]
        targets = gateway["qqbot"].get("targets", [])
        users = [str(item["id"]) for item in targets if item.get("type") == "user"]
        groups = [str(item["id"]) for item in targets if item.get("type") == "group"]
        return {
            "CVM_INSTANCE_ID": str(gateway["instance-id"]), "CVM_REGION": str(gateway["region"]),
            "CVM_SECRET_ID": env["GW_SECRET_ID"], "CVM_SECRET_KEY": env["GW_SECRET_KEY"],
            "QQBOT_SECRET_ID": env["QQBOT_SECRET_ID"], "QQBOT_SECRET_KEY": env["QQBOT_SECRET_KEY"],
            "QQBOT_TARGET_TYPE": "user" if users else "group", "QQBOT_TARGET": users[0] if users else "",
            "QQBOT_GROUP_TARGET": groups[0] if groups else "",
            "QQBOT_ADMINS": " ".join(map(str, gateway["qqbot"].get("admins", []))),
            "MC_PACK": pack, "MC_PORT": result.stdout.strip(),
            "MC_ADDR": f"{self.doc['manager']['host']}:{result.stdout.strip()}",
            "MC_AUTO_SCHEDULE": "1" if schedule["enabled"] else "0",
            "MC_POWER_START": str(config.get_path(self.doc, "gateway.schedule.power-start")),
            "MC_PLAY_START": str(config.get_path(self.doc, "gateway.schedule.start")),
            "MC_SHUTDOWN_START": str(config.get_path(self.doc, "gateway.schedule.end")),
            "MC_PLAY_END": str(config.get_path(self.doc, "gateway.schedule.end")),
            "MC_POWEROFF_AT": str(config.get_path(self.doc, "gateway.schedule.poweroff")),
            "MC_NOTICE_MINUTES": str(config.get_path(self.doc, "gateway.schedule.notices")),
            "MC_MANAGER_DIR": self.directory("manager"),
        }

    def deploy_gateway(self) -> None:
        directory, target = self.directory("gateway"), self.target("gateway")
        log = directory + "/deploy.log"
        runtime_env = self.gateway_env()
        ssh(target, ["mkdir", "-p", directory + "/data"])
        ssh(target, ["bash", "-lc", f": > {shellq(log)}"])
        self.install_dependencies("gateway", log)
        archive = archive_tree(GATEWAY, exclude={"data", ".env"})
        try:
            scp(archive, f"{target}:{directory}/.gateway-upload.tar.gz")
            script = f"find {shellq(directory)} -mindepth 1 -maxdepth 1 ! -name data ! -name .env ! -name deploy.log -exec rm -rf -- {{}} +; tar -xzf {shellq(directory + '/.gateway-upload.tar.gz')} -C {shellq(directory)}; rm -f {shellq(directory + '/.gateway-upload.tar.gz')}"
            ssh(target, ["bash", "-lc", script])
        finally:
            shutil.rmtree(archive.parent, ignore_errors=True)
        with tempfile.TemporaryDirectory(prefix="mc-serverpack-") as temp:
            output = Path(temp) / "gateway.env"
            output.write_text("".join(f"{key}={value}\n" for key, value in runtime_env.items()), encoding="utf-8")
            scp(output, f"{target}:{directory}/.env")
        ssh(target, ["bash", "-lc", f"cd {shellq(directory)} && docker compose config --quiet && docker compose up -d --build --remove-orphans qqbot"])

    def bot(self, action: str, lines: int = 100) -> None:
        directory, target = self.directory("gateway"), self.target("gateway")
        commands = {
            "status": ["docker", "compose", "ps", "qqbot"],
            "restart": ["docker", "compose", "restart", "qqbot"],
            "stop": ["docker", "compose", "stop", "qqbot"],
            "logs": ["docker", "compose", "logs", f"--tail={lines}", "qqbot"],
            "follow": ["docker", "compose", "logs", "-f", f"--tail={lines}", "qqbot"],
        }
        ssh(target, ["bash", "-lc", f"cd {shellq(directory)} && {shelljoin(commands[action])}"], tty=action == "follow")

    def gateway(self, action: str) -> None:
        directory, target = self.directory("gateway"), self.target("gateway")
        if action == "stop":
            self.remote_manager(["stop", config.choose_pack()])
            extra = ["-e", "POWEROFF_WAIT_MAX=0"]
        else:
            extra = []
        command = ["docker", "compose", "exec", "-T", *extra, "qqbot", "bash", "/app/cvm-power.sh", action]
        ssh(target, ["bash", "-lc", f"cd {shellq(directory)} && {shelljoin(command)}"])

    def check(self, target: str) -> int:
        failed = False
        for command in ("ssh", "scp"):
            ok = executable(command)
            print(f"[{'OK' if ok else 'FAIL'}] {command}")
            failed |= not ok
        print("[OK] config.yml")
        names = ("manager", "gateway") if target == "all" else (target,)
        for name in names:
            if name == "manager":
                local_ok = (MANAGER / "manager.sh").is_file()
                remote_check = "command -v docker >/dev/null && docker compose version >/dev/null && command -v python3 >/dev/null && python3 -c 'import sys, yaml; raise SystemExit(sys.version_info < (3, 8))' && command -v crontab >/dev/null"
            else:
                local_ok = (GATEWAY / "compose.yml").is_file() and ENV_FILE.is_file()
                remote_check = "command -v docker >/dev/null && docker compose version >/dev/null"
            print(f"[{'OK' if local_ok else 'FAIL'}] {name} 本地部署文件")
            failed |= not local_ok
            try:
                ssh(self.target(name), ["bash", "-lc", remote_check], capture=True)
                print(f"[OK] {name} 远端环境")
            except (CommandError, RuntimeError) as exc:
                print(f"[FAIL] {name} 远端环境（可直接 deploy 自动安装）：{exc}")
                failed = True
        return int(failed)


def shellq(value: str) -> str:
    import shlex
    return shlex.quote(value)


def shelljoin(values: list[str]) -> str:
    return " ".join(shellq(value) for value in values)
