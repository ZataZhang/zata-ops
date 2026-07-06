# 后端系统设计

本文档描述 `zata-ops` 仓库内 **新后端**（从 `zata_code_template` 迁移而来的 FastAPI 应用）的整体设计、四层结构与入口边界。它是 `docs/ai-standards/architecture.md` 的项目级权威补足：标准页是 AI 通用速查，本页是该仓库后端的实现细节与现状说明。

> **迁移状态（截至本文档首次落地）**
>
> - ✅ 已迁移：`src/backend/` 下四层源代码（`api/`、`core/`、`engines/`、`infrastructure/`、`shared/`、`main.py`、`Dockerfile`）
> - ✅ 已迁移：前端 `frontend-admin/`、`frontend-public/` 与根 `pnpm-workspace.yaml`
> - ✅ 已合并：后端 Python 依赖落入 `[project.optional-dependencies].backend`
> - ✅ 已迁移：`alembic/`、`alembic.ini`、`config.toml`、`.env.example` 后端段；`settings.py` 默认 `service_name` 已改为 `zata-ops-backend`
> - ✅ 已接线：`just run [backend|frontend|all|docker]` / `just down` 直接面向三端结构（已替换原模板的 `frontend_dir=` 单前端写法）
> - ⏳ 未迁移：`deploy/vps-traefik/`、`hooks/shared/` 下后端相关 hook、`scripts/build/`、`tests/` 下后端测试
> - ⏳ 未启用：后端首次冷启动还需在 `.env.local` 补 `REDIS_URL` 等运行时凭据；本文档随后续迁移轮次刷新

## 顶层视角

仓库是一个聚合体：

| 子项目 | 路径 | 形态 | 角色 |
|---|---|---|---|
| CLI 运维工具 | `src/zata_ops/` | Python · Typer | `zata-ops` 主入口；提供 db / env / logs / dashboard / tunnel |
| 后端 API 服务 | `src/backend/` | Python · FastAPI | 给前端提供 HTTP API；本轮尚未启动 |
| 管理后台前端 | `frontend-admin/` | TS · Vite + React 19 + TanStack Router | 给管理员使用 |
| 公共门户前端 | `frontend-public/` | TS · Next.js 16 + React 19 | 给终端用户使用 |

`just run` / `just run docker` 在模板仓里同时启动 backend + 两个前端；在 zata-ops 仓库，CLI 与后端尚未接入同一份 `just run`，本轮不做合并。

## 后端四层结构

详见 `docs/ai-standards/architecture.md` 的依赖方向表，本节只补充 `src/backend/` 当前的具体落点：

| 层 | 路径 | 关键子目录 / 模块 |
|---|---|---|
| 接入层 | `src/backend/api/` | `agent_router.py`、`auth_router.py`、`session_router.py`、`tool_router.py`、`workflow_router.py`、`health_router.py`、`metrics_router.py`、`admin/`、`middleware/` |
| 核心编排层 | `src/backend/core/` | `agent/`、`auth/`（含 `service.py`、`directory.py`、`models.py`）、`session/`、`workflow/`、`use_cases/` |
| 平台能力层 | `src/backend/engines/` | `rag/`、`skills/`（含 `registry/tool_registry.py`、`tools/` 等） |
| 基础设施层 | `src/backend/infrastructure/` | `auth/`（bcrypt、redis_client、redis_session_store）、`config/`、`http_clients/`、`models/llm_client.py`、`persistence/`、`logger.py`、`helpers.py` |

依赖方向固定为 `api -> core -> engines -> infrastructure`，禁止反向 import；跨层契约通过 `core/shared/interfaces/` 抽象。

## 入口与 Composition Root

`src/backend/main.py` 是 FastAPI 应用的 composition root，模板仓采用"`_run_migrations() -> create_app() -> app`"三段式：

- 启动时自动 `alembic upgrade head`
- 在 `app.state` 上挂载 auth / agent / session / workflow / tool 仓库与 LLM client
- 注册所有 router 与可选中间件（RequestContext / PrometheusMetrics）
- 通过环境变量 `PORT` 决定监听端口

迁移到 zata-ops 后，zata-ops 自身的 `main.py`（CLI wrapper）与 `src/backend/main.py`（FastAPI 装配）会**并存**：

- `uv run zata-ops` → 走 `src/zata_ops/cli.py`
- `uv run uvicorn backend.main:app`（待 alembic 与 settings 就绪后）→ 走 `src/backend/main.py`

## 配置与密钥分层

后端配置沿用模板仓约定（`pydantic-settings` + `.env` 覆盖），由 `src/backend/infrastructure/config/settings.py` 加载。`.env.local`（不进版本控制）继续享有最高覆盖优先级，与 zata-ops 的 CLI 共用同一套加载顺序。

## 前端子项目

两个前端包独立运行：

```bash
pnpm install                  # 在仓库根目录（pnpm workspace）
pnpm --filter frontend-admin dev    # 启动 admin：http://localhost:5173
pnpm --filter frontend-public dev   # 启动 public：http://localhost:3000
```

后端真正可调用前，前端的 `BACKEND_PORT` / API base URL 默认值需要等后端 settings 落地后再校对。

## 后续迁移路线（待办）

已完成的轮次：

- ✅ **alembic & 迁移**：`alembic.ini`、`alembic/env.py`、`alembic/script.py.mako`、`alembic/versions/*.py` 已就位
- ✅ **配置与密钥**：`config.toml` 已合入；`.env.example` 后端段已合并
- ✅ **justfile 接线**：`just run` / `just down` 已私有化改写，端口三 key 写入 `vanta-run.env`

未完成的轮次：

1. **deploy & hooks**：搬 `deploy/vps-traefik/`、`hooks/shared/` 中后端相关 hook、`scripts/build/`
2. **测试**：搬 `tests/` 下后端测试，新增 `backend` 测试组；为 alembic 迁移添加 SQL 回放回归

每轮结束后在本文档顶部 **迁移状态** 段更新 ✅/⏳ 列表，保持文档与代码同步。

## just run 与端口管理

`just run` / `just down` 是 zata-ops 仓库的开发入口，直接面向 **一个后端 + 两个前端** 的真实结构，不再走模板的 `frontend_dir=` 单前端模式。

```bash
just run                          # backend + frontend-admin + frontend-public 并发
just run backend                  # 只起后端 (uvicorn backend.main:app)，默认 8000
just run frontend                 # 起两个前端（admin 5173 + public 3000，并发）
just run docker                   # 通过 docker compose 拉起整套（待后续轮补 docker-compose.yml）
just run backend backend_port=8010 \
                frontend_admin_port=5179 frontend_public_port=3001
just down                         # 按 remembered 端口停掉 backend + 两前端
just down backend                 # 只停后端
just down frontend                # 只停两前端
```

`just run backend` 依赖 Python 的 `[backend]` extra（`uv sync --extra backend`），未安装时会给出友好提示；前端依赖 `pnpm install` 在仓库根完成。

`just run` 把端口状态写入仓库根的 `.env.run-state`（与 `justfile.shared` 上游约定一致），新增三个 key：`BACKEND_PORT` / `FRONTEND_ADMIN_PORT` / `FRONTEND_PUBLIC_PORT`，`just down` 自动复用。

后端运行入口（与 `just run backend` 等价）：

```bash
uv run uvicorn backend.main:app --host 0.0.0.0 --port ${BACKEND_PORT:-8000} --reload
```