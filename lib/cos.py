#!/usr/bin/env python3
"""Generate a short-lived Tencent COS upload or download URL."""

import argparse
import os


def die(message: str) -> None:
    raise SystemExit("错误: " + message)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="生成 COS 一次性上传或下载签名链接")
    parser.add_argument("name", nargs="?", help="文件名或完整对象路径")
    parser.add_argument("--get", action="store_true", help="生成下载链接；默认生成上传链接")
    parser.add_argument("--expire", type=int, help="有效期秒数")
    parser.add_argument("--force", action="store_true", help="允许覆盖已经存在的上传对象")
    parser.add_argument("--url-only", action="store_true", help="只输出 URL")
    args = parser.parse_args(argv)
    if args.get and args.force:
        parser.error("--get 与 --force 不能同时使用")
    required = ("TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY", "REGION", "COS_BUCKET")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        die(".env 缺少 " + ", ".join(missing))
    name = (args.name or os.environ.get("COS_OBJECT_NAME", "")).strip().lstrip("/")
    if not name:
        parser.error("请提供对象名，或在 .env 填写 COS_OBJECT_NAME")
    prefix = os.environ.get("COS_UPLOAD_PREFIX", "").strip().strip("/")
    key = name if "/" in name or not prefix else f"{prefix}/{name}"
    expire = args.expire if args.expire is not None else int(os.environ.get("COS_URL_EXPIRE") or 3600)
    if expire <= 0:
        die("有效期必须是正整数秒数")
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError:
        die("缺少依赖，请安装 cos-python-sdk-v5")
    region, bucket = os.environ["REGION"].strip(), os.environ["COS_BUCKET"].strip()
    client = CosS3Client(CosConfig(
        Region=region, SecretId=os.environ["TENCENTCLOUD_SECRET_ID"].strip(),
        SecretKey=os.environ["TENCENTCLOUD_SECRET_KEY"].strip(), Scheme="https",
    ))
    try:
        exists = client.object_exists(Bucket=bucket, Key=key)
    except Exception as exc:
        die(f"访问 COS 失败：{exc}")
    if args.get and not exists:
        die(f"COS 中不存在 {key}")
    if not args.get and exists and not args.force:
        die(f"COS 中已存在 {key}；确认覆盖时添加 --force")
    url = client.get_presigned_url(
        Method="GET" if args.get else "PUT", Bucket=bucket, Key=key, Expired=expire,
    )
    if args.url_only:
        print(url)
    else:
        print(f"地域：{region}")
        print(f"存储桶：{bucket}")
        print(f"路径：{key}")
        print(f"钥匙：'{url}'")


if __name__ == "__main__":
    main()
