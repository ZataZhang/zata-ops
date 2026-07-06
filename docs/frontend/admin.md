# 前端 · Admin

本前端位于仓库根 `frontend-admin/`（Vite + React 19 + TanStack Router），主要面向管理员。

包内完整说明见 [`frontend-admin/README.md`](https://github.com/your-org/zata-ops/blob/main/frontend-admin/README.md)。详细架构与编码规范仍在包内文档维护，本索引页只列出常用入口。

## 启动

```bash
pnpm install
pnpm --filter frontend-admin dev
# 默认 http://localhost:5173
```

构建与测试：

```bash
pnpm --filter frontend-admin build
pnpm --filter frontend-admin test
```

后端真正接入前，前端的 `BACKEND_PORT` / API base URL 默认值需要等后端 settings 落地后再校对。