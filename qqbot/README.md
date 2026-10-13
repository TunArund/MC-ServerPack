# Gateway、QQ Bot 与 CVM 控制器

`qqbot/` 部署到常在线 gateway，负责 QQ 命令、定时开关 mc-server 和告警。运行状态
保存在 `data/`，重新部署不会覆盖。

## 配置

gateway 的 SSH、CVM 和调度配置位于根 `config.yml`；腾讯云与 QQ Bot 密钥位于根
`.env`。远端 `qqbot/.env` 是部署时生成的运行产物，不手工维护。

```yaml
gateway:
  host: gateway.example
  user: steam
  dir: /home/steam/MC-ServerPack/qqbot
  region: ap-nanjing
  instance-id: ins-xxx
  schedule:
    enabled: true
    start: 20:00
    start-elast: 5
    end: 23:00
    end-elast: 3
    notify-elast: 60
    notify-step: 2
```

`start-elast` 是为云主机和 Minecraft 启动预留的分钟数；`end-elast` 是关服后的
正常关机宽限。计划关机时，manager 先保存并停服，gateway 随后调用
`StopInstances` 并复查；只有确认 Minecraft 已停止时才允许强制关机兜底。

## CAM 最小权限

```text
cvm:DescribeInstances
cvm:StartInstances
cvm:StopInstances
tat:RunCommand
tat:DescribeInvocationTasks
```

## 命令

```text
deploy gateway
gateway config
gateway start|stop|status|guard
bot status|restart|stop|logs|follow [行数]
```

当前一个 gateway 固定控制一个整合包。manager 本身仍可管理多个整合包。
