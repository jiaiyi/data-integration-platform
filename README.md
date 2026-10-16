# 生产级 ETL-Agent 数据集成平台

把一句业务话（「把 MySQL 的订单同步到 Doris，金额必须为正，邮箱脱敏，错误数据单独保存，每天凌晨执行」）
变成**受管、可审查、可审批、可执行、可回放**的数据任务。

需求与设计依据：[`specs/PRD.md`](specs/PRD.md)（v1.2）、[`specs/SPEC.md`](specs/SPEC.md)（v1.1）、
[`prototype/README.md`](prototype/README.md)（高保真原型）。

> **跨会话接手请先读 [`HANDOVER.md`](HANDOVER.md)** —— 那里有需求满足度对照（哪些刻意未满足）、
> 下一步顺序、环境状态，以及只在开发过程中得出、不写下来就会被重新推翻的设计判断。

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

端口：postgres `5432`、redis **`16379`**（容器内仍是 6379）、mysql-e2e `13306`、
MinIO `9000`（控制台 `9001`）、Doris FE `8030/9030`、SeaTunnel REST `8080`。

**redis 为什么发布到 16379**：宿主机 6379 常被本机原生 Redis 占用，而 Docker 在这种情况下**不报错** ——
容器照样 `Up (healthy)`，只是端口发布静默失效（`docker inspect` 里 `NetworkSettings.Ports` 为空、
`docker compose port redis 6379` 返回 `:0`），此时 `redis://localhost:6379` 连到的是那个原生 Redis，
不是容器。改用 16379 后与 `DIP_REDIS_URL` 默认值一致，且不干扰本机既有服务。
启动后核对发布是否生效（这条排查对任何端口冲突都适用）：

```bash
docker compose -f infra/compose.yaml port redis 6379   # 期望 0.0.0.0:16379；返回 :0 即发布未生效
docker ps --format "{{.Names}} {{.Ports}}"             # 期望 0.0.0.0:16379->6379/tcp
```


## 验证

```bash
uv run ruff check .            # 静态检查
uv run mypy                    # 严格类型检查
uv run pytest                  # 单元测试 + 契约测试
uv run pytest -m integration   # 真库集成测试（需 --profile e2e 起的 PostgreSQL / MySQL / MinIO）
docker compose -f infra/compose.yaml config --quiet   # 编排语法
cd frontend && npm run build   # 前端构建（含 vue-tsc）
```

> 集成测试在对应服务未启动时**整组跳过**，不会把「没跑」伪装成「跑过了」。
> 拉不到镜像的数据源（Oracle / SQL Server / Hive / HDFS / ClickHouse）无法真库验证，
> 由 `tests/test_probe_contracts.py` 用 SQL 契约测试覆盖（无无界 COUNT、有受限行数、拒绝注入）。

### 本机联调已踩过的坑

| 现象 | 原因与处置 |
|------|-----------|
| 请求挂到超时，服务端日志却显示已处理完 | 本机配了系统 HTTP 代理，回环请求被代理劫走。客户端脚本加 `trust_env=False` |
| 连接常常先卡几秒才通 | Windows 把 `localhost` 优先解析到 IPv6 `::1`，而 Docker 只发布 IPv4。默认连接串一律写 `127.0.0.1`（实测 Redis 5.03s vs 0.02s） |
| `redis://localhost:6379` 连上了却不是本项目的数据 | 宿主 6379 被本机原生 Redis 占用，且 Docker 不报错、只是端口发布静默失效。本项目固定发布到 16379 |
| 中文数据落库变成 `å¼ ä¸‰` | MySQL 入口脚本默认不以 utf8mb4 执行 init SQL。`infra/e2e/mysql-init.sql` 顶部已加 `SET NAMES utf8mb4;` |
| Worker 报 `'NoneType' object has no attribute 'send'` | 每个任务用 `asyncio.run` 新建事件循环，而连接池绑在旧循环上。`asyncio_compat` 改为进程级复用同一事件循环 |
| 内网地址（Vault / Webhook / REST 数据源）报 502 或连不上 | 系统 HTTP 代理把外发请求劫走 —— **顺带会把 Vault token、鉴权头交给未知中间人**。平台主动外发一律 `trust_env=False` |
| 新增一个枚举值后 PostgreSQL 插入报 `value too long` | 枚举列未指定长度时按「建表当时最长枚举值」生成 VARCHAR(n)。已固定为 `ENUM_COLUMN_LENGTH=64` 并由 `tests/test_schema_contract.py` 守住 |
| SeaTunnel REST 按旧文档写 `/hazelcast/rest/maps/*` 全 404 | 2.3.13 的端点挂在**根**下（`/overview`、`/running-jobs`、`/encrypt-config`…）。另外 CLI 的 `--check` 校验的是**集群配置**、对作业配置是空操作，没有作业级 dry-run |
| `/encrypt-config` 收 HOCON 文本一律回 `Invalid JSON format in request body.` | 该接口只吃 JSON 配置文档，且 `source`/`transform`/`sink` 必须是**插件对象数组**（传对象会报 `LinkedHashMap cannot be cast to ArrayList`）。候选 HOCON 由 `pyhocon` 转换后再送引擎 |
| Worker 报 `Psycopg cannot use the 'ProactorEventLoop'` | Windows 上 asyncpg 与 psycopg **只能取交集**：asyncpg 两种循环都能跑，psycopg（LangGraph 的 Checkpoint 库）明确拒绝 Proactor。`asyncio_compat` 因此改用 `WindowsSelectorEventLoopPolicy` |
| 引擎 `/submit-job` 回 500 且消息为空 | `format` 是**查询参数**、请求体是 **HOCON 原文**。塞进 JSON 字段会落到引擎自己会 NPE 的分支。另外 HOCON 同一行连写多个字段也会解析失败 |
| Celery 任务报 `This event loop is already running` | 在**已经是协程**的函数里调用 `run_in_transaction`（它内部走 `run_async` → `run_until_complete`）会二次进同一个循环。协程内部直接开会话，不要绕回同步桥接 |
| Outbox 的重试上限永远触发不了 | 领用用的是 SQL 侧自增 `attempts = attempts + 1`，会话里的对象还是旧值。再查一次时要加 `populate_existing=True` |
| 提交/回滚后读 ORM 属性抛 `MissingGreenlet` | 对象过期后再读属性会触发**同步**惰性加载。要用的主键在提交前先取出来 |

## 交付里程碑

任务编号对应 SPEC §10，当前进度：

| 阶段 | 任务 | 状态 |
|------|------|------|
| M0 工程基座 | T-01 工程骨架 / T-02 配置边界 / T-03 基础设施编排 / T-04 可观测基座 | 已完成 |
| M1 领域与身份 | T-05 枚举与状态机 / T-06 Harness 值对象 / T-07 数据模型与迁移 / T-08 身份与项目治理 / T-09 职责分离强制 | 已完成 |
| M2 受管数据上下文 | T-10 探查契约 / T-11 七个探查器 / T-12 ClickHouse(GAP-01) / T-13 脱敏 / T-14 SecretProvider / T-15 凭据全生命周期 / T-16 文件与对象存储(GAP-02) / T-17 REST API(GAP-03) | 已完成 |
| M3 生成与制品治理 | T-18 生成契约 / T-19 状态机 / T-20 确定性门禁 / T-21 真实引擎检查 / T-22 不可变版本 / T-23 调度策略 / 生成链路 API 与 Worker 接线 | **已完成** |
| M4 Harness 安全内核 | T-24 工具契约清单 / T-25 PDP / T-26 数据分类 / T-27 Tool Broker / T-28 Capability 签名 / T-29 Replay Guard / T-30 事务 Outbox / T-31 证据账本 / T-32 失败关闭 | **已完成** |
| M5 三阶段协议与执行 | T-33 ~ T-39 **已完成**；T-36 通用副作用框架与执行相关 API **已完成**；T-40 交付与受管文件输出 **待做** | 进行中 |
| M6 质量、监督与治理 | T-41 ~ T-46 | 待开工 |
| M7 控制台 | T-47 ~ T-53 | 待开工 |
| M8 评测与安全进化 | T-54 ~ T-58、T-63 | 待开工 |
| M9 端到端与交付 | T-59 ~ T-62 | 待开工 |

`api/routes/` 下已建全部 18 个 Router 模块并挂载：已实现端点的是 `health` / `projects` /
`connections` / `file_assets` / `pipelines` / `agent_runs` / `operations` / `executions` /
`approvals` / `schedules` / `side_effects` 与四个 `secret_*`；
`rollbacks` / `cleanups` / `safety_evolution` 仍是空的 `APIRouter` 对象，
由对应里程碑填充（**不留返回 501 的假端点**）。

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
