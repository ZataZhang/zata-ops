# VPS 初始化

`zata-ops env provision` 是对 `install-docker-traefik.sh` 等 shell 模板的
轻封装。命令默认只渲染 SSH 调用计划，不执行；只有显式去掉 `--dry-run` 时
才真正通过本地 `ssh` 命令落到目标主机。

## 为服务器添加 SSH 公钥

`scripts/ssh/add-authorized-key.sh` 可将本机的 SSH 公钥添加到目标服务器用户的
`~/.ssh/authorized_keys`：

```bash
scripts/ssh/add-authorized-key.sh example.com "$(cat ~/.ssh/id_ed25519.pub)"
```

默认以 `root` 用户和本机 `~/.ssh/id_ed25519` 私钥登录，超时为 20 秒。可通过
`SSH_USER`、`SSH_KEY`、`SSH_PORT` 和 `SSH_TIMEOUT` 环境变量覆盖这些设置。脚本默认
只使用公钥认证；首次配置、尚未能用密钥登录时，可显式启用交互式密码引导：

```bash
SSH_ALLOW_PASSWORD=1 scripts/ssh/add-authorized-key.sh example.com "$(cat ~/.ssh/id_ed25519.pub)"
```

密码引导默认超时为 60 秒。重复添加同一公钥会跳过，不会在 `authorized_keys` 中
重复写入。

## Dry run

```bash
zata-ops env provision \
    --host example.com \
    --user deploy \
    --ssh-key ~/.ssh/cd-my-app \
    --profile vps-traefik \
    --traefik-network traefik \
    --acme-email ops@example.com \
    --dry-run
```

输出包含：

- 要执行的 `ssh` argv（含 `bash -s` here-doc 包裹的脚本内容）。
- `bootstrap.sh` 仍建议在工作站本地执行，命令计划中只会展示其长度。

## 真正执行

去掉 `--dry-run` 后，命令会按顺序执行计划中的 `argv`：

```bash
zata-ops env provision --host example.com --user deploy --ssh-key ~/.ssh/cd-my-app \
    --profile vps-traefik --acme-email ops@example.com
```

执行前请确保本机已配置好 `~/.ssh/known_hosts` 与 SSH key 权限。

## ACME 邮箱修复

旧版 `install-docker-traefik.sh` 留下占位邮箱时，Let's Encrypt 会拒签：

```bash
zata-ops env fix --host example.com --user root --email ops@example.com --dry-run
zata-ops env fix --host example.com --user root --email ops@example.com
```

命令会在服务器上：

1. 定位 Traefik 安装目录。
2. 重写 `traefik.yml` 的 `email:` 字段。
3. 删除旧的 `acme.json`。
4. 重启 Traefik 并等待新证书签发。
