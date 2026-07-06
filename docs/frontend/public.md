# 前端 · Public

本前端位于仓库根 `frontend-public/`（Next.js 16 + React 19），面向终端用户。

包内完整说明见 [`frontend-public/README.md`](https://github.com/your-org/zata-ops/blob/main/frontend-public/README.md)。详细架构与编码规范仍在包内文档维护，本索引页只列出常用入口。

## 启动

```bash
pnpm install
pnpm --filter frontend-public dev
# 默认 http://localhost:3000
```

构建与 lint：

```bash
pnpm --filter frontend-public build
pnpm --filter frontend-public lint
```

后端真正接入前，前端的 API base URL 默认值需要等后端 settings 落地后再校对。