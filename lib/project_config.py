#!/usr/bin/env python3
"""Read and validate the repository-level config.yml."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yml"
PACK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


class ConfigError(RuntimeError):
    pass


def time_text(value: object, field: str) -> str:
    # PyYAML 1.1 reads an unquoted `20:00` as the sexagesimal integer 1200.
    if type(value) is int and 0 <= value < 24 * 60:
        value = f"{value // 60:02d}:{value % 60:02d}"
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        raise ConfigError(f"{field} 必须是 HH:MM")
    return value


def mapping(value: object, field: str, optional: bool = False) -> dict:
    if value is None and optional:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{field} 必须是对象")
    return value


def nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} 必须是非空字符串")
    return value.strip()


def load() -> dict:
    if yaml is None:
        raise ConfigError("缺少 PyYAML")
    try:
        doc = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError("缺少 config.yml；请从 config.yml.example 复制") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"config.yml 读取失败：{exc}") from exc
    root = mapping(doc, "config.yml")
    manager = mapping(root.get("manager"), "manager")
    modpack = mapping(manager.get("modpack"), "manager.modpack")
    nonempty(manager.get("region"), "manager.region")
    nonempty(manager.get("instance-id"), "manager.instance-id")
    nonempty(manager.get("host"), "manager.host")
    directory = nonempty(manager.get("dir"), "manager.dir")
    if not directory.startswith("/"):
        raise ConfigError("manager.dir 必须是绝对路径")
    maximum = modpack.get("max-running", 1)
    if type(maximum) is not int or maximum < 1:
        raise ConfigError("manager.modpack.max-running 必须是正整数")
    if modpack.get("overnum-action", "reject") not in ("reject", "rotate"):
        raise ConfigError("manager.modpack.overnum-action 只能是 reject 或 rotate")
    nonempty(modpack.get("mem-limit"), "manager.modpack.mem-limit")
    gateway = mapping(root.get("gateway"), "gateway", optional=True)
    if gateway:
        nonempty(gateway.get("host"), "gateway.host")
        gateway_dir = nonempty(gateway.get("dir"), "gateway.dir")
        if not gateway_dir.startswith("/"):
            raise ConfigError("gateway.dir 必须是绝对路径")
        schedule = mapping(gateway.get("schedule"), "gateway.schedule")
        if type(schedule.get("enabled")) is not bool:
            raise ConfigError("gateway.schedule.enabled 必须是 true 或 false")
        time_text(schedule.get("start"), "gateway.schedule.start")
        time_text(schedule.get("end"), "gateway.schedule.end")
        for key in ("start-elast", "end-elast", "notify-elast", "notify-step"):
            if type(schedule.get(key)) is not int or schedule[key] < 1:
                raise ConfigError(f"gateway.schedule.{key} 必须是正整数")
        qqbot = mapping(gateway.get("qqbot"), "gateway.qqbot")
        targets = qqbot.get("targets", [])
        admins = qqbot.get("admins", [])
        if not isinstance(targets, list) or not isinstance(admins, list):
            raise ConfigError("gateway.qqbot.targets/admins 必须是数组")
        for item in targets:
            if not isinstance(item, dict) or item.get("type") not in ("user", "group"):
                raise ConfigError("QQ Bot target.type 只能是 user 或 group")
            nonempty(item.get("id"), "gateway.qqbot.targets[].id")
    cos = mapping(root.get("cos"), "cos", optional=True)
    if cos:
        nonempty(cos.get("region"), "cos.region")
        nonempty(cos.get("bucket"), "cos.bucket")
        nonempty(cos.get("prefix", ""), "cos.prefix")
        if type(cos.get("expire")) is not int or cos["expire"] < 1:
            raise ConfigError("cos.expire 必须是正整数")
    return root


def get_path(doc: dict, path: str) -> object:
    if path in ("gateway.schedule.power-start", "gateway.schedule.poweroff"):
        schedule = doc["gateway"]["schedule"]
        base_key = "start" if path.endswith("power-start") else "end"
        elast_key = "start-elast" if base_key == "start" else "end-elast"
        base = time_text(schedule[base_key], f"gateway.schedule.{base_key}")
        direction = -1 if base_key == "start" else 1
        minutes = int(base[:2]) * 60 + int(base[3:]) + direction * schedule[elast_key]
        return f"{minutes % 1440 // 60:02d}:{minutes % 60:02d}"
    if path == "gateway.schedule.notices":
        schedule = doc["gateway"]["schedule"]
        current, step, values = schedule["notify-elast"], schedule["notify-step"], []
        while current >= 1:
            values.append(str(current)); current //= step
        return " ".join(values)
    value: object = doc
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ConfigError(f"config.yml 缺少 {path}")
        value = value[part]
    if path in ("gateway.schedule.start", "gateway.schedule.end"):
        return time_text(value, path)
    return value


def choose_pack() -> str:
    packs = sorted(path.parent.name for path in (ROOT / "manager/modpacks").glob("*/config.yml"))
    private = [pack for pack in packs if pack != "template"]
    choices = private or packs
    if len(choices) != 1:
        raise ConfigError("gateway 需要唯一整合包；当前请只保留一个非 template 整合包")
    return choices[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    get = sub.add_parser("get"); get.add_argument("path")
    sub.add_parser("validate")
    sub.add_parser("gateway-pack")
    targets = sub.add_parser("targets"); targets.add_argument("type", choices=("user", "group"))
    sub.add_parser("admins")
    args = parser.parse_args()
    doc = load()
    if args.action == "get":
        value = get_path(doc, args.path)
        if isinstance(value, bool):
            print(str(value).lower())
        elif isinstance(value, (dict, list)):
            print(json.dumps(value, ensure_ascii=False))
        else:
            print(value)
    elif args.action == "gateway-pack":
        print(choose_pack())
    elif args.action == "targets":
        values = doc.get("gateway", {}).get("qqbot", {}).get("targets", [])
        print(" ".join(str(item["id"]) for item in values if item.get("type") == args.type))
    elif args.action == "admins":
        values = doc.get("gateway", {}).get("qqbot", {}).get("admins", [])
        print(" ".join(map(str, values)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
