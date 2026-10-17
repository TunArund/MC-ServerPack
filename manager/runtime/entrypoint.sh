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

# Direct Java launches share one editable argument file. Read one literal JVM
# argument per line here instead of relying on Java's @argfile support, so the
# same contract also works with the Java 8 runtime image.
if [[ "${1:-}" == "java" && -f user_jvm_args.txt ]]; then
  java_command="$1"
  shift
  jvm_args=()
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    jvm_args+=("$line")
  done < user_jvm_args.txt
  set -- "$java_command" "${jvm_args[@]}" "$@"
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
