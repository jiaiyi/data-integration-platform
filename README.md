# 生产级 ETL-Agent 数据集成平台

把一句业务话（「把 MySQL 的订单同步到 Doris，金额必须为正，邮箱脱敏，错误数据单独保存，每天凌晨执行」）
变成**受管、可审查、可审批、可执行、可回放**的数据任务。

需求与设计依据：[`specs/PRD.md`](specs/PRD.md)（v1.2）、[`specs/SPEC.md`](specs/SPEC.md)（v1.1）、
[`prototype/README.md`](prototype/README.md)（高保真原型）。

## 架构一句话

**控制面 / 数据面分离 + 分层架构 + 确定性安全内核（Agent Harness）。**

不是微服务 —— 安全性依赖本地事务原子性（`Consume Capability + Create Run + Write Outbox` 必须同事务），
微服务会把它降级为跨服务最终一致。见 SPEC ADR-01。

```
api / *_tasks  →  application  →  domain  ←  infrastructure
（交互边界）      （用例编排）    （纯规则）    （Port 实现）
```

**依赖方向铁律**：`domain` 层不导入 FastAPI、Celery、SQLAlchemy 或任何数据库驱动，
也不做 I/O。这条保证业务规则可以脱离框架独立测试。

## 目录

| 路径 | 内容 |
|------|------|
| `specs/` | 需求（PRD）与技术设计（SPEC）定稿 |
| `prototype/` | 高保真原型（`.pen` 源文件、预览 HTML、截图、生成脚本） |
| `Docker镜像/` | 离线镜像附件（基础 ≈155 MB / E2E 扩展 ≈7.6 GB）。**共 7.3 GB，不入仓库**，需另行获取 |
| `src/data_integration_platform/` | 控制面服务端 |
| `src/data_integration_platform/domain/` | 枚举、状态机、Harness 值对象与监督契约（无框架依赖） |
| `src/data_integration_platform/infrastructure/` | ORM 模型、数据库、探查器与引擎适配器 |
| `src/data_integration_platform/application/` | 用例服务与 Port 定义 |
| `src/data_integration_platform/api/` | 路由、请求响应契约、身份与项目上下文 |
| `frontend/` | Vue 3 + TypeScript 控制台（五视图） |
| `alembic/` | 数据库迁移 |
| `infra/` | Compose 编排、初始化 SQL、E2E 固件 |
| `tests/` | 单元测试与分层仿真（L0/L1/L2） |

## 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | **3.12.x** | 不含 3.13（PRD §5.4） |
| uv | ≥ 0.12 | 依赖与虚拟环境管理 |
| Node.js | ≥ 20 | 前端构建 |
| Docker Desktop + Compose | — | 本机常驻依赖与按需 E2E 环境 |

## 本地启动

```bash
# 1. 依赖
uv sync
cd frontend && npm install && cd ..

# 2. 配置
cp .env.example .env        # 按需修改；生产边界校验见 config.py

# 3. 常驻依赖（postgres + redis）
docker compose -f infra/compose.yaml up -d

# 4. 数据库迁移
uv run alembic upgrade head

# 5. 控制面
uv run dip-api              # http://localhost:8000  /health

# 6. 前端
cd frontend && npm run dev  # http://localhost:5173
```

### 真实链路（E2E）

离线镜像**只替代镜像下载**，不替代 Docker 运行时、Compose、数据卷初始化与迁移（PRD ENV-01~03）。
加载后必须显式禁止在线拉取与构建，并带上 `e2e` profile（不带 profile 不会创建 mysql / doris / seatunnel）：

```bash
# 镜像附件不入仓库（7.3 GB），需先放入 Docker镜像/ 目录
docker load --input ./Docker镜像/生产级ETL-Agent-v0.1.0-基础镜像.tar.gz
docker load --input ./Docker镜像/生产级ETL-Agent-v0.1.0-E2E扩展镜像.tar.gz

docker compose -f infra/compose.yaml --profile e2e up -d --no-build --pull never
uv run alembic upgrade head
```

端口：postgres `5432`、redis `6379`、mysql-e2e `13306`、Doris FE `8030/9030`、SeaTunnel REST `8080`。
`5432` / `6379` 被占用会导致容器创建阶段直接失败，先排查再启动。

## 验证

```bash
uv run ruff check .            # 静态检查
uv run mypy                    # 严格类型检查
uv run pytest                  # 单元测试
docker compose -f infra/compose.yaml config --quiet   # 编排语法
cd frontend && npm run build   # 前端构建（含 vue-tsc）
```

## 交付里程碑

任务编号对应 SPEC §10，当前进度：

| 阶段 | 任务 | 状态 |
|------|------|------|
| M0 工程基座 | T-01 工程骨架 / T-02 配置边界 / T-03 基础设施编排 / T-04 可观测基座 | 已完成 |
| M1 领域与身份 | T-05 枚举与状态机 / T-06 Harness 值对象 / T-07 数据模型与迁移 / T-08 身份与项目治理 / T-09 职责分离强制 | 已完成 |
| M2 受管数据上下文 | T-10 ~ T-17（含 GAP-01 ClickHouse / GAP-02 S3-MinIO / GAP-03 REST API） | 待开工 |
| M3 生成与制品治理 | T-18 ~ T-23 | 待开工 |
| M4 Harness 安全内核 | T-24 ~ T-32 | 待开工 |
| M5 三阶段协议与执行 | T-33 ~ T-40 | 待开工 |
| M6 质量、监督与治理 | T-41 ~ T-46 | 待开工 |
| M7 控制台 | T-47 ~ T-53 | 待开工 |
| M8 评测与安全进化 | T-54 ~ T-58、T-63 | 待开工 |
| M9 端到端与交付 | T-59 ~ T-62 | 待开工 |

`api/routes/` 下已建全部 18 个 Router 模块并挂载，但只有 M0/M1 覆盖的
`health` 与 `projects` 实现了端点；其余模块当前只有空的 `APIRouter` 对象，
由对应里程碑填充。

## 明确不做（避免误判）

- **不重写数据集成引擎**：数据搬运交给 SeaTunnel Zeta。
- **不让模型拥有最终裁决权**：模型只产出候选，确定性代码与数据库约束才是边界。
- **不交付生产部署形态**：高可用、TLS、网络隔离、外部 KMS、WORM 证据存储、企业 SSO/IAM
  均不在本期范围（PRD §10.4、SPEC §4.3）。
- **不承担等保测评与合规认证**：审计留存 ≥ 180 天，安全能力按「工程上可讲清、可验证」交付（PRD §5.2 Q4）。

## 已知边界

签名哈希链可发现**平台数据库内**证据被改写，但不是外部公证系统；若攻击者同时控制数据库与签名私钥，
仍需 KMS 与 WORM 存储。事务 Outbox 保证本地业务事实与待投递命令原子落库，
**不提供** PostgreSQL 与任意外部数据库的跨系统事务。
