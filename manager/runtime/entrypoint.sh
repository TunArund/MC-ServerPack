#!/usr/bin/env bash
set -Eeuo pipefail

SERVER_DIR="${MC_SERVER_DIR:-/server}"
CONSOLE_PIPE="${MC_CONSOLE_PIPE:-/tmp/mc-manager-console.pipe}"
STOP_TIMEOUT="${MC_STOP_TIMEOUT:-180}"

if [ "$#" -eq 0 ]; then
  echo "错误：整合包 compose.yml 没有设置 Minecraft 启动命令" >&2
  exit 2
fi
[ -d "$SERVER_DIR" ] || {
  echo "错误：服务端工作目录不存在：$SERVER_DIR" >&2
  exit 1
}

rm -f "$CONSOLE_PIPE"
mkfifo -m 600 "$CONSOLE_PIPE"

# Java 的标准输入连接到这个命名管道；manager.sh、QQ Bot 都可安全地写入命令。
exec 3<>"$CONSOLE_PIPE"

cd "$SERVER_DIR"

resolve_auto_command() {
  local installer jar args_file
  local -a jars=() argument_files=()
  local -a automatic_jvm_args=(
    -XX:InitialRAMPercentage=25.0
    -XX:MaxRAMPercentage=75.0
  )

  mapfile -d '' -t argument_files < <(
    find . -type f -name unix_args.txt -print0 | sort -z
  )
  if (( ${#argument_files[@]} > 1 )); then
    echo "错误：检测到多个 unix_args.txt，无法自动选择启动入口" >&2
    return 2
  fi
  if (( ${#argument_files[@]} == 1 )); then
    args_file="${argument_files[0]#./}"
    set -- java "${automatic_jvm_args[@]}" "@$args_file" nogui
    AUTO_COMMAND=("$@")
    return 0
  fi

  mapfile -d '' -t jars < <(
    find . -maxdepth 1 -type f -name '*.jar' -print0 | sort -z
  )
  if (( ${#jars[@]} != 1 )); then
    echo "错误：自动启动要求 /server 中恰有一个顶层 JAR；当前找到 ${#jars[@]} 个" >&2
    return 2
  fi
  jar="${jars[0]#./}"
  installer=0
  if unzip -Z1 "$jar" | grep -qx 'install_profile.json'; then
    installer=1
  fi
  if (( installer )); then
    echo "检测到服务端安装器，执行一次性安装：$jar --installServer"
    java -jar "$jar" --installServer
    mapfile -d '' -t argument_files < <(
      find . -type f -name unix_args.txt -print0 | sort -z
    )
    if (( ${#argument_files[@]} != 1 )); then
      echo "错误：安装完成后未找到唯一的 unix_args.txt" >&2
      return 2
    fi
    args_file="${argument_files[0]#./}"
    AUTO_COMMAND=(java "${automatic_jvm_args[@]}" "@$args_file" nogui)
    return 0
  fi
  AUTO_COMMAND=(java "${automatic_jvm_args[@]}" -jar "$jar" nogui)
}

if [[ "${1:-}" == "mc-auto" ]]; then
  AUTO_COMMAND=()
  resolve_auto_command
  set -- "${AUTO_COMMAND[@]}"
fi

echo "启动 Minecraft：$*"
"$@" <&3 &
server_pid=$!
stopping=0

stop_server() {
  local deadline
  [ "$stopping" -eq 0 ] || return 0
  stopping=1
  kill -0 "$server_pid" 2>/dev/null || return 0

  echo "收到停止信号，向 Minecraft 控制台发送 stop"
  printf 'stop\n' >&3 || true
  deadline=$((SECONDS + STOP_TIMEOUT))
  while kill -0 "$server_pid" 2>/dev/null && [ "$SECONDS" -lt "$deadline" ]; do
    sleep 1
  done

  if kill -0 "$server_pid" 2>/dev/null; then
    echo "警告：Minecraft 未在 ${STOP_TIMEOUT}s 内退出，将由 Docker 结束容器" >&2
    exit 124
  fi
}

trap stop_server TERM INT
set +e
wait "$server_pid"
status=$?
set -e
# Docker 的 TERM 会中断上面的 wait；如果 stop 已让 Java 正常退出，则容器也应成功退出。
if [ "$stopping" -eq 1 ] && ! kill -0 "$server_pid" 2>/dev/null; then
  status=0
fi
trap - TERM INT
exec 3>&-
rm -f "$CONSOLE_PIPE"
exit "$status"
