# FreshAI 开发者 OpenBao 开通

`just openbao-onboard` 为同事创建个人 OpenBao 用户，并将对方的公网出口 IP 追加到该实例的 Traefik API 白名单。用户通过个人账号登录后，本地 FreshAI 便可读取开发环境的 Nacos 密码和 API Key。

## 运行命令

在 `zata-ops` 根目录运行。将实例名、同事的用户名和公网出口 IP 填入变量，先预览，再加 `--apply` 执行。

```bash
just openbao-onboard "$INSTANCE" "$USERNAME" "$PUBLIC_IP"
just openbao-onboard "$INSTANCE" "$USERNAME" "$PUBLIC_IP" --apply
```

- 实例名取自 `config/private/openbao/` 的目录名，须与该目录 `ssh.conf` 中的 Host 别名一致。实例还须已有 `ingress.env`、`server.crt` 和 `operator.json.age`。
- 用户名使用小写字母、数字、连字符或下划线，以小写字母或数字开头。公网 IP 填单个地址，脚本将 IPv4 按 `/32`、IPv6 按 `/128` 加入名单。
- 运维电脑须有 `uv`、`just`、`ssh`、`ssh-keygen`、`bao` 和 `age`。SSH 使用私有配置中的身份。解密使用 `ZATA_BACKUP_IDENTITY` 指定的 SSH 密钥，默认使用本机 SSH Ed25519 密钥。首次执行前请确认私有配置可以正常加密备份。
- 目标机器须能运行 `python3`，且 SSH 身份能读取和原子替换 OpenBao 专用的 Traefik 动态路由文件。入口应先按 [Traefik 部署说明](openbao-traefik.md) 建好。

预览会通过 SSH 只读核对线上路由和本地白名单，并显示两边是否需要追加 IP。应用时，脚本在首次写入本地私有配置之前创建加密备份。若备份命令提示需手动复制，按[私有配置备份说明](private-inventory.md)处理。

## 执行结果与交接

脚本通过 SSH 隧道登录 `operator`，借助仅有目标用户名读取和创建权限的临时 AppRole 建号。脚本先核对现有 `app-freshai-dev-read` 策略只允许读取 FreshAI 开发 runtime，再签发初始密码，验证个人登录、开发读取权限以及测试和生产读取均被拒绝。个人 token 初始有效期为 24 小时，硬性上限为七天。临时角色、策略和核验会话随后清理。最后，脚本更新线上路由文件并回读验证，再同步本地白名单。线上文件发生并发变化时会中止更新。

初始密码只保存为 age 加密文件，命令输出会给出路径。macOS 上可用下面的命令复制密码，通过私密渠道交给同事。其他系统可将解密结果导入自己的密码管理器。

```bash
age -d -i "${ZATA_BACKUP_IDENTITY:-$HOME/.ssh/id_ed25519}" \
  "config/private/openbao/$INSTANCE/users/$USERNAME.json.age" |
  jq -jr '.password' | pbcopy
```

将用户名和实例的 HTTPS 地址一并告诉同事。同事在本机配置 FreshAI 的 `BAO_ADDR` 和 `BAO_TOKEN_PATH`，在他自己的出口网络上运行下列命令。登录后 token 会存入他本机的配置路径。应用启动即可验证公网白名单、登录和取密都能正常工作。

```bash
just dev-login "$USERNAME"
just run
```

## 重试与排查

- 建号和入口更新是两个独立操作。若执行中断，使用**同一个实例、用户名和 IP**再次运行。脚本会沿用加密归档中的密码并验证已有账号，且会补齐尚未同步的白名单。发现同名用户但缺少对应归档，或归档密码已失效时会停止。
- 如果线上和本地白名单还有其他差异，先比对两边的其他来源并同步。脚本允许当前目标 IP 在某一边已存在，遇到其他不一致则停止，避免覆盖已有放行规则。
- 执行成功代表脚本已回读路由文件并通过 SSH 管理入口验证了账号。若同事仍无法登录，请从他的网络请求 `https://<OPENBAO_DOMAIN>/v1/sys/health`。若返回 `403`，优先核对实际出口 IP、IPv4/IPv6 选择和 Traefik 是否加载了更新。
- 更换出口时再次运行会添加新地址。旧地址何时撤销按[入口配置指南](openbao-traefik.md)办理。

接口依据已于 2026-11-23 核对。[OpenBao AppRole API](https://openbao.org/docs/api/auth/approle/) · [OpenBao Userpass API](https://openbao.org/docs/api/auth/userpass/) · [Traefik IPAllowList](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/ipallowlist/)
