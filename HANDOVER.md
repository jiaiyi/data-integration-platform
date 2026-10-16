# 交接文档

> 用途：跨会话延续本项目开发。**先读本文件，再读 `README.md`（进度表 + 本机联调坑）
> 与 `specs/SPEC.md §10`（任务分解）。**
> 最后更新：2026-10-16，对应提交 `e43618c`（M5 进行中）。

---

## 1. 一句话现状

| 阶段 | 状态 |
|---|---|
| M0 工程基座 / M1 领域与身份 / M2 受管数据上下文 | **完成，并已真实验证** |
| M3 生成与制品治理 | **完成**，生成链路已用真实 DeepSeek + 真实 SeaTunnel 2.3.13 端到端跑通 |
| M4 Harness 安全内核（T-24 ~ T-32） | **完成**（内核与契约测试都在） |
| M5 三阶段协议与执行 | **进行中**：T-33/34/35/37/38/39 已完成并已用真实引擎跑通；**剩 T-36、T-40 与执行 API** |

**接手时先确认仓库是好的：**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest                                   # 期望 731 passed
uv run pytest -m integration                    # 期望 28 passed（需要 Compose 全栈）
docker compose -f infra/compose.yaml --profile e2e ps   # 期望 8 个服务在跑
```

`tests/integration/` 下的用例在对应服务没起时**整组跳过** —— **跳过不等于通过**，
别把跳过当证据。

---

## 2. 需求满足度对照（**最重要的一节**）

防止把「没做」当成「做了」。以下是**刻意未满足或只做了一半**的需求。

### 2.1 M5 尚未完成的部分（下一步就要做）

| 需求 | 状态 | 说明 |
|------|------|------|
| **FR-57 / FR-23**「所有副作用走同一受管入口」 | **只做了一半** | 生成与执行链路已经走 Broker / Capability / Outbox / Ledger（T-35、T-37）。但**九类受管副作用**（执行取消、Doris 回滚/清理、凭据迁移/轮换暂存/验证/激活/回退/销毁）**仍走 M2 各自的既有路径**，没有统一到 T-36 的入口。**在此之前不得声称 FR-57 已满足。** |
| **SPEC API-05 ~ API-08** 执行相关端点 | **未做** | `executions` / `side_effects` / `approvals` 路由仍是空壳。准备单的创建与查询、具名审批决策、Commit、执行查询**都没有 HTTP 入口**，只能由服务层调用。§4 记录了真实烟测是怎么直接调服务层跑的。 |
| **T-40 交付与受管文件输出** | **未做** | 执行结果目前只是引擎侧的作业状态；把结果交付成受管文件对象还没做。 |
| **执行状态的持续对账** | **未做** | `SeaTunnelExecutionEngine.status` 能读引擎状态，但**没有周期性对账任务**：提交之后没人把引擎的推进回写到 `execution_runs`。SPEC T-38 的「状态未知先对账」只做了一半（投影函数与 `None` 语义在，驱动它的扫描不在）。 |

### 2.2 能力边界（**不是没做完，是刻意如此**，别写成「已满足」）

| 项 | 状态 | 说明 |
|------|------|------|
| **FR-35 真实引擎检查** | 已做，但**不等价于 SPEC 写的「引擎 `--check`」** | 那个机制在 2.3.13 上不存在（见 §4）。本层做的是「引擎可用性 + 引擎自己的配置解析器判配置**文档结构**」，**不校验连接器选项** —— 选项级校验只在提交时的作业解析里发生。 |
| **FR-28 生成后端可切换** | 切换点存在，第二后端未接入 | `create_generation_adapter` 遇到 `seatunnel-cli` 会**明确报错**，不静默降级（与探查器注册表同款姿态）。 |
| **FR-33 逐行 LLM Transform** | 只做了门禁侧 | 默认严格关闭、开启需独立密钥与出口白名单已落配置与门禁；能力本身未实现（决策 Q7）。 |
| **FR-49 策略包「签名」** | 只做到版本化 | 规则集在代码里（`application/harness/pdp.py`），决策记录 `policy_bundle_version` 与全部命中规则。**自签自验证明不了什么**，所以没做签名；要能热更与回滚，得先把规则集外置成清单文件再对文件签名。 |
| **`harness_public_keys` 表** | 改用配置 | SPEC §8.1 写的是 Worker 挂一张公钥映射表，实际做成 `DIP_HARNESS_SIGNING_PUBLIC_KEYS`（`key_id:base64url`，逗号分隔，支持轮换期多把）。理由：M4 里没有这张表的写入方与运维端点；代价是加钥匙要重新部署 —— 对信任根可接受。**这是与 SPEC 的差异，别以为表已经建了。** |
| **证据签名与能力签名共用一把密钥** | **M5 收尾前必须拆开** | `ledger.append` 只要求一个 `SignerPort`，当前沿用与能力令牌同一把。Worker 一旦开始产生执行证据，**必须为证据单独配一把密钥**：否则为了让 Worker 签证据，就得把签发能力的私钥也交给它，「Worker 在结构上无法自行签发能力」这条边界会直接消失。（当前只有 Commit 写账本，发生在持私钥的一侧，所以还没踩到。） |
| **凭据占位符的命名约定** | **规格里没有，本项目自定** | 固定写成 `<角色>_<连接器>_<字段>`（如 `${SOURCE_MYSQL_PASSWORD}`）。生成阶段把可用名字喂给模型，执行阶段按**同一份清单**（`placeholders.CONNECTOR_FIELDS`）替换，**表外的名字一律拒提交**。带角色前缀是为了消歧：源端与目标端都可能是 MySQL。 |
| **远端文件只做前缀采样** | 刻意的硬失败边界 | 超过上限（默认 8 MiB）时，Parquet/ORC/Excel/PDF 这类「结构在文件尾部」的格式**直接报错**，而不是读一半再推断（那会产出假结构）；文本类走前缀读取时**行数留空**，不拿部分字节算出的数冒充。见 `infrastructure/remote_probe.py::plan_remote_read`。 |

### 2.3 验证覆盖不到的（**别写成「已真实验证」**）

| 项 | 状态 |
|------|------|
| **T-15 的 Vault** | 本机拉不到 `hashicorp/vault` 镜像，集成测试用一个实现 KV v2 语义的本地 HTTP 服务代替。**Vault 客户端代码被真实请求路径验证过，但没对真实 Vault 验证过。** |
| ClickHouse / Oracle / Hive / HDFS | 镜像拉不到，真库验证做不了。由 `tests/test_probe_contracts.py` 的 SQL 契约测试覆盖（无无界 COUNT、有受限行数、拒绝标识符注入、注册表覆盖全类型）。 |
| M7 控制台（T-47~T-53） | 只有五视图骨架 + 原型设计令牌，页面逻辑未做。后端接口目前**没有前端在用**。 |

---

## 3. 下一步该做什么（按顺序）

### 第一步：T-36 通用副作用框架

把九类操作（`domain/side_effects.py::SideEffectOperation`：执行取消、Doris 回滚/清理、
凭据迁移/轮换暂存/验证/激活/回退/销毁）统一到同一套「申请—审批—提交」。

**九类操作现在的真实状态**（这一步之前先看清，别以为是从零开始、也别以为已经接通）：

| 操作 | 现在在哪 |
|---|---|
| `SECRET_MIGRATION` / `SECRET_ROTATION_STAGE` / `_VERIFY` / `_ACTIVATE` / `SECRET_ROTATION_ROLLBACK` / `SECRET_DESTRUCTION` | **已实现**，但走的是 M2 自己那条路：`application/secret_{migrations,rotations,rotation_rollbacks,destructions}.py`，各自有「申请→具名审批→执行」的表与状态机，审批人要持 `SECURITY_APPROVER` 槽。**没有**经过 Capability / Outbox / Broker。 |
| `EXECUTION_CANCEL` | 引擎侧能力有了（`SeaTunnelExecutionEngine.cancel`），但**没有任何受管路径**，也没有 API。 |
| `DORIS_ROLLBACK` / `DORIS_CLEANUP` | **什么都没有**，`rollbacks` / `cleanups` 路由是空壳。 |

**因此 T-36 有两件事**：① 把已经实现的六类**改成经过准备单/审批/能力令牌**（不是重写业务逻辑 ——
别把已经验收过的东西推倒）；② 给另外三类补上实现。`HarnessSideEffectPreparation` /
`HarnessSideEffectApproval` / `HarnessSideEffectCapabilityGrant` 三张表 M1 就建好了，
`application/harness/` 里的 Broker / Capability / Grants / Outbox 都能直接复用，缺的是
**把它们串起来的用例层**与 `side_effects` 路由。

### 第二步：执行相关的 API 端点（SPEC API-05 ~ API-08）

- `POST /api/v1/pipeline-versions/{id}/execution-preparations`（Prepare，入参只有 `idempotency_key`，**其余事实服务端冻结**）
- `POST /api/v1/execution-preparations/{id}/decisions`（具名审批：`role` / `decision` / `reason`）
- `POST /api/v1/execution-preparations/{id}/commit`（**无入参**，服务端重建事实）
- `GET /api/v1/execution-runs/{id}`（35 列投影，只返回脱敏聚合指标）

另外还有两块一起补：**版本的审批决策**（`PipelineVersionService.decide_approval` 已实现，
但没有 HTTP 入口）与**调度配置**（`schedules` 路由是空壳，`application/schedules.py` 已实现）。
补上它们，生成与执行整条链路才在 HTTP 上闭合。

**`operations` 路由已有 4 个端点**（M2 的凭据盘点与到期告警），可以照它抄。

照现有路由的写法（`api/routes/pipelines.py` / `agent_runs.py`）照抄即可：鉴权用 `ActorId`，
事务用 `async with session.begin()`，投递用 `celery_app.dispatch`。

### 第三步：T-40 交付与受管文件输出

执行结果交付成受管文件对象，同样受治理。可以复用 `application/file_assets.py` 的落盘
与校验路径。

### 第四步：M6（质量、监督与治理，T-41 ~ T-46）

其中 **T-43 运行监督引擎** 与 §2.1 里「执行状态对账」是同一片区域，一起做更省事。

### 收尾要做的（每轮结束时）

- **控制面库需要重置**：现在里面有前几轮烟测留下的 213 个项目（见 §5）。
  清库需要 `DROP SCHEMA public CASCADE` 这类操作，**本轮被权限拦截过，没有执行** ——
  交给用户确认后再做，别擅自清。
- 停掉临时的 API / Worker 进程（`Get-CimInstance Win32_Process` 按 `dip-` 匹配后杀）。
- 删掉临时脚本与 `.env.smoke` 之类文件。

---

## 4. 已完成的里程碑（回溯，压缩）

### 4.1 各阶段落在哪里

| 阶段 | 主要落地 |
|---|---|
| M0 | `config.py`（生产边界启动期校验）、`infra/compose.yaml`、`observability.py`、18 个 Router 骨架 |
| M1 | `domain/{enums,state_machine,harness,supervision}.py`、`infrastructure/models.py`（全部表）、身份与项目治理、SOD 强制 |
| M2 | 七个探查器 + `probe_registry`、ClickHouse(GAP-01)、S3/MinIO(GAP-02)、REST API(GAP-03)、脱敏、凭据全生命周期 |
| M3 | `agent/graph.py`（LangGraph）、`application/policy.py`（确定性门禁）、`engine_check.py` + `seatunnel_engine_check.py`、`application/versions.py`（不可变版本）、`application/schedules.py`、生成链路 API + `generation_tasks.py` |
| M4 | `application/harness/` 九个模块（清单/PDP/分类/签名/重放/Broker/Outbox/账本/失败关闭） |
| M5（部分） | `application/harness/{preparation,approval,commit,engine,placeholders}.py`、`execution_tasks.py`、`beat_tasks.py`、`infrastructure/seatunnel_execution.py` |

### 4.2 实测结论（**排查出来的，别再重新踩**）

**SeaTunnel 2.3.13 的 REST 契约**（文档写的与实测不一致）：

- 端点挂在**根**下，不是 `/hazelcast/rest/maps/*`。
- CLI 的 `--check` **对作业配置是空操作**（语法损坏、插件不存在的配置都返回 0）——
  它校验的是**集群配置**。SPEC §10 写的「引擎 `--check`」在 2.3.13 上不成立。
- 提交是 `POST /submit-job?format=HOCON&jobName=<名字>`，**`format` 是查询参数、请求体是 HOCON 原文**。
  照文档写成 `{"config": "...", "configFormat": "HOCON"}` 会落到默认的 JSON 分支，
  那条分支在 2.3.13 上引擎自己会 NPE（`ConfigShadeUtils.processConfig` 对 `sink` 取空值），
  返回 500 且消息为空 —— 排查时会被这个空消息误导半天。
- HOCON **同一行连写多个字段**（`a = 1 b = 2`）会解析失败，必须用换行或逗号分隔。
- **没有可用的「按 jobId 查状态」接口**（`/job-info` 只允许 GET 却又要读请求体）。
  状态得去 `/running-jobs`、`/pending-jobs`、`/finished-jobs` 三个列表里找。
- `/encrypt-config` 能用引擎自己的解析器**无副作用**地解析配置，但只吃 **JSON 配置文档**
  （HOCON 文本回 `Invalid JSON format in request body.`），且 `source`/`transform`/`sink`
  必须是**插件对象数组**（传对象报 `LinkedHashMap cannot be cast to ArrayList`）。
- 引擎给的时间是 `yyyy-MM-dd HH:mm:ss` 且**不带时区**，按 `DIP_SEATUNNEL_TIMEZONE` 解释。

**生成链路（M3）**：真实 DeepSeek 生成 → 确定性门禁 → 真实引擎检查 → 冻结 →
`design view` 从**冻结制品**读回（与执行用的是同一份字节）。澄清答案随任务载荷走
（`Command(resume=...)`），中间状态归 `dip_checkpoint` 库，不落业务库。

**执行链路（M5）**：真实跑通 `prepare → 审批 → commit → outbox 认领 → worker 验签 →
物化 → 提交 → 引擎 FINISHED`（job `1163409713448222721`）。

> 这次烟测是**直接调服务层**跑的（`SessionFactory` + `application/harness/*` + `execution_tasks._submit`），
> 因为 §3 第二步要补的那批 HTTP 端点还没写。补完之后重跑一遍走 HTTP 的版本，才算把接口层也验了。
> 烟测脚本当时放在仓库外的临时目录，没入库。

---

## 5. 环境状态（本机，2026-10-16）

| 项 | 状态 |
|---|---|
| Git | `main` → `git@github.com:jiaiyi/data-integration-platform.git`，SSH 可直接 push（`gh` 未安装） |
| Docker 镜像 | 基础镜像 + E2E 扩展镜像（Doris FE/BE、SeaTunnel）均已加载 |
| 容器 | 当前 9 个在跑（本项目 8 个 + 一个无关的 `customer-service-mysql`）。起全栈：`docker compose -f infra/compose.yaml --profile e2e up -d --no-build --pull never` |
| 端口 | postgres `5432`、redis **`16379`**、mysql-e2e `13306`、minio `9000/9001`、Doris FE `8030/9030`、SeaTunnel `8080` |
| 控制面库 | 迁到 `20261016_0003`（head）。**数据不干净**：有前几轮烟测留下的 **213 个项目**、4 条 `agent_runs`、1 份准备单、1 条执行记录。清库需 `DROP SCHEMA` 级别的操作，**本轮被权限拦截，未执行**。 |
| 制品目录 | `var/artifacts/` 下有 6 个文件（前几轮冻结的制品，已被 gitignore） |
| 注册表 | **Docker Hub 不通**，只有 `mcr.microsoft.com` 可达 → ClickHouse/Oracle/Hive/HDFS/Vault 镜像拉不到 |
| `.env` | 已配好 LLM 接入与一套本地 Ed25519 签名密钥对；`DIP_BOOTSTRAP_ENABLED=false` |

**用户的提交要求：一个里程碑（M）提交推送一次**，不要每个子任务都提交。

---

## 6. 只在对话里说过、但必须延续的设计判断

这些是排查出来的结论，不写下来新会话会重新推导甚至推翻。

1. **枚举列长度固定为 `models.ENUM_COLUMN_LENGTH = 64`。** 不指定长度时 SQLAlchemy 按
   「建表当时最长枚举值」生成 `VARCHAR(n)`，于是新增一个更长的枚举值会在 PostgreSQL 插入失败
   （实测：`audit_events.event_type` 是 varchar(19)，新增值 23 字符）。
   **SQLite 不校验长度，所以只在真库上暴露。** `tests/test_schema_contract.py` 守住这条。
2. **平台主动外发一律 `trust_env=False`**（Vault / Webhook / REST 数据源 / WebHDFS / LLM / 引擎）。
   走系统代理不仅连不上内网地址，**还会把 Vault token、API Key、鉴权头交给未知中间人**。
3. **连接串一律写 `127.0.0.1` 而不是 `localhost`。** Windows 优先解析到 IPv6 `::1`，
   而 Docker 只发布 IPv4（实测 Redis 差 250 倍，且无超时的客户端会直接挂住）。
4. **Worker 必须进程级复用同一个事件循环**（`asyncio_compat`）。每个任务 `asyncio.run`
   新建循环会让连接池绑在旧循环上，报 `'NoneType' object has no attribute 'send'`。
   **Windows 上这个循环用 Selector 而不是 Proactor**：asyncpg 两种都能跑（实测），
   而 psycopg（LangGraph 的 Checkpoint 库）明确拒绝 Proactor —— Selector 是唯一交集。
5. **在协程里不要用 `run_in_transaction`**：它内部是 `run_async` → `run_until_complete`，
   而协程已经在那条循环上跑着，再进去就是「事件循环已在运行」。
   协程内部直接 `async with SessionFactory()`。
6. **未预期的异常也要先落事实**。生成任务的 `_invoke` 一度只捕获 `DomainError`，
   结果 Checkpointer 的实现异常直接抛出、事务回滚，运行**永久停在 RUNNING**。
   与探查那边「失败也要先记录终态」是同一条要求。
7. **分级序位只有一份**（`domain/harness.SENSITIVITY_ORDER`）：PDP 判风险与分类报告算总级
   都读它。之前两处各写了一遍，`UNKNOWN` 在一处按「受限」、在另一处按「秘密」——
   这类重复不会立刻报错，只会在某个字段上悄悄给出不同结论。
   `UNKNOWN` 刻意排在「受限」与「秘密」之间：比已知受限更该被盯着，但等同秘密会让
   任何未分类数据都出不了门。**整表分级因此是保守的**：只要还有未分类字段，整表就是 UNKNOWN。
8. **事实推导只能有一条路径**（`preparation.derived_facts_for`）。Prepare、审批前校验、
   Commit 各推一遍的话，任何一处漏了某个输入（Commit 忘了带连接，踩过）都会让指纹漂移，
   而漂移看起来像「事实被改过」，排查方向全错。
9. **SQLAlchemy 的 SQL 侧自增表达式不会刷新会话里的对象**。Outbox 领用用
   `attempts = attempts + 1` 后，会话里的对象仍读旧值，导致重试上限永远触发不了。
   再查一次时加 `populate_existing=True`。
10. **提交/回滚之后读 ORM 属性会触发同步惰性加载**，在 async 会话里直接抛 `MissingGreenlet`。
    要用的主键在提交前先取出来。
11. **`.delay()` 不能在 async 路由里直接调用**（同步阻塞会卡住整个事件循环），
    统一走 `celery_app.dispatch` 卸载到线程。
12. **`session.begin()` 之前不能先查库**：`AsyncSession` 首次查询会自动开启事务，
    之后再 `begin()` 抛 `A transaction is already begun`（踩过一次，已有回归测试）。
13. **凭据操作不做自动重试**（可能已部分生效，重试会放大问题）；
    **告警投递可以重试**（接收端按 `delivery_id` 幂等）。两者刻意相反。
14. **引擎可搬运 ≠ 可登记**：`REST_API` 满足 FR-103（登记 + 探查），但 SeaTunnel 没有对应
    Connector，**不能作为管道端点** —— 门禁在 `ENGINE_MOVABLE_CONNECTORS` 里拦住它。
15. **不要写「占位路由」或「返回 501 的假端点」**。未实现的模块只留空的 `APIRouter`
    并挂载；伪造端点会在下个里程碑全部删掉，是纯噪音。
16. **`as_utc()` 统一时间口径**：SQLite 读回的 `DateTime(timezone=True)` 不带 tzinfo，
    直接与 aware 时间比较会抛 `TypeError`。领域比较一律先归一化。
17. **`CapabilityMismatch` 刻意不继承 `DomainError`**（它是安全边界上的拒绝）。
    Worker 里必须**显式接住**，否则它会穿透成一个没有结论的异常而不是一次干净的拒绝。
18. **croniter 默认把秒放在最后**。PRD 写的是 `0 0 2 * * *`（秒在前），不显式传
    `second_at_beginning=True` 会被读成「每月 2 日 0 点」——任务照跑、时间全错、不报任何错。
    `croniter.is_valid()` **也要**传这个参数，否则 `0 30 3 * * *` 会被判非法。
19. **工具清单的自洽性在构造期就查**：补偿工具必须真在目录里（否则「可回滚」是空话）、
    同名工具不得重复登记、同名工具的所有版本效果必须一致（否则「这工具危不危险」没有确定答案）。

---

## 7. 验证的分工（别把跳过当通过）

| 手段 | 覆盖什么 | 不覆盖什么 |
|---|---|---|
| 单元 / 契约测试（703 条） | 状态机、门禁、PDP、分类、签名、重放、Outbox、账本、脱敏、格式推断、SQL 生成、身份与职责分离 | 任何真实外部系统 |
| `pytest -m integration`（28 条） | 真实 PostgreSQL / MySQL / MinIO / Doris / SeaTunnel 提交与状态；未起服务时**整组跳过** | 需要 Docker Hub 镜像的数据源、真实 Vault、真实 LLM |
| 服务层端到端烟测 | 业务事实的完整推进（生成、三阶段协议、Outbox → Worker → 引擎） | 同上；**且它绕过 HTTP 层**，接口契约由 API 测试单独覆盖 |

**演示时最有说服力的两条命令**：

```bash
uv run pytest -m integration                    # 真实引擎的提交与状态投影
uv run pytest tests/test_tool_broker.py -q      # 「无绕过路径」的证据是执行器调用次数为 0
```
