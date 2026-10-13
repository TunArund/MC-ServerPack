#!/bin/bash
# ══════════════════════════════════════════════════════════════════
#  mc-msg.sh —— 取一条发往 QQ 的文案并填上占位符
#
#  这个文件是【被 source 的】, 不是拿来执行的:
#      . /path/to/mc-msg.sh
#      . /path/to/messages.env      # 由 messages-to-env.py 生成
#      msg STATUS_RUNNING players=3
#
#  文案本身住在 messages.json 里 (单一来源), 容器启动时摊平成
#  messages.env —— 所以运行时【不需要 python3】, 只管查变量、做替换。
#
#  为什么不用 envsubst / sed:
#      · envsubst 认的是 $VAR, 而文案里用 {名字}, 那才是 json 那边自然的写法;
#      · sed 要拿用户数据当正则, 值里一个 / 或 & 就会出岔子。
#      bash 自己的 ${var//pat/rep} 是字面量替换, 不解释正则, 正合适。
#
#  取不到键时会把键名原样打出来 (而不是打印空) —— 空消息发出去是静默失败,
#  而看得见的 MSG_XXX 至少一眼知道是哪条文案丢了。
# ══════════════════════════════════════════════════════════════════

msg() {  # $1=键名(不带 MSG_ 前缀, 大小写随意)  其余: k=v
  local key tmpl kv k v
  key="MSG_$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
  shift

  # 间接展开: ${!key} 取的是"名字存在 key 里的那个变量"
  tmpl="${!key-}"
  if [ -z "$tmpl" ]; then
    printf 'MSG_%s' "${key#MSG_}"
    return 0
  fi

  # 按名字逐个替换, 与顺序无关 —— 位置参数那种写法(printf 的 %s)要求调用方
  # 记住文案里占位符的先后, 改一次文案就可能悄悄错位。
  for kv in "$@"; do
    k="${kv%%=*}"
    v="${kv#*=}"
    tmpl="${tmpl//\{$k\}/$v}"
  done
  printf '%s' "$tmpl"
}
