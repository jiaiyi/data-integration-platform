# 交接文档

> 用途：跨会话延续本项目开发。**先读本文件，再读 `README.md`（进度表 + 本机联调坑）
> 与 `specs/SPEC.md §10`（任务分解）。**
> 最后更新：2026-10-16，M5 进行中（T-33 ~ T-39 与 T-36、执行 API 已完成）。

---

## 1. 一句话现状

| 阶段 | 状态 |
|---|---|
| M0 工程基座 / M1 领域与身份 / M2 受管数据上下文 | **完成，并已真实验证** |
| M3 生成与制品治理 | **完成**，生成链路已用真实 DeepSeek + 真实 SeaTunnel 2.3.13 端到端跑通 |
| M4 Harness 安全内核（T-24 ~ T-32） | **完成**（内核与契约测试都在） |
| M5 三阶段协议与执行 | **完成**：T-33 ~ T-40 全部落地（T-36 通用副作用框架、执行 API、T-40 交付）。**两条尾巴刻意留给 M6**：执行状态的持续对账、Doris 回滚/清理的真实执行（见 §2.1） |

**接手时先确认仓库是好的：**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest                                   # 期望 742 passed
uv run pytest -m integration                    # 期望 28 passed（需要 Compose 全栈）
uv run alembic current                          # 期望 20261016_0004 (head)
docker compose -f infra/compose.yaml --profile e2e ps   # 期望 8 个服务在跑
```

`tests/integration/` 下的用例在对应服务没起时**整组跳过** —— **跳过不等于通过**，
别把跳过当证据。

---

## 2. 需求满足度对照（**最重要的一节**）

防止把「没做」当成「做了」。以下是**刻意未满足或只做了一半**的需求。

### 2.1 M5 未满足的部分（刻意留给 M6，**别把它们当成没做完**）

| 需求 | 状态 | 说明 |
|------|------|------|
| **执行状态的持续对账** | **未做** | `SeaTunnelExecutionEngine.status` 能读引擎状态，但**没有周期性对账任务**：提交之后没人把引擎的推进回写到 `execution_runs`。SPEC T-38 的「状态未知先对账」只做了一半（投影函数与 `None` 语义在，驱动它的扫描不在）。**取消也一样**：`CANCELLING` 之后要有人确认引擎真的停了才进 `CANCELLED`，那个确认者也还没写。**T-40 的自动交付**同样要挂在它上面（见 §3 第一步）。 |
| **DORIS_ROLLBACK / DORIS_CLEANUP 的真实执行** | **只做了受管入口** | 准备单/审批/能力/租约齐备，执行器接口 `DorisMaintenancePort` 已定；`create_doris_maintenance()` 会**明确报错**（不静默降级）。真实换名/删表属 M6 的 T-45（SPEC 的划分）。`rollbacks` / `cleanups` 路由因此仍是空壳。 |
| **T-40 的交付对象** | 只交付**执行报告**，不交付结果数据 | 引擎只回作业状态、不回结果数据（数据直接落在 Doris 里），所以现实的交付物是执行自己产生的聚合事实（状态/指标/失败归类/诊断/监督/质量）。要交付真实结果数据集，得先有一条从 Doris 导出的路径 —— 那属 M6 的 T-42/T-45 区域。**别把「交付报告」说成「交付数据结果」。** |

### 2.1.1 T-36 落地时的三个定案（**都与 SPEC 字面有出入，是有意为之**）

| 项 | 定案 | 理由 |
|---|---|---|
| **轮换的三段（暂存/验证/激活）** | **共用一份准备单**，`operation` 记 `SECRET_ROTATION_STAGE` | 拆成三张会让一次人工审批变成三次，而三者动的是同一把凭据、同一个连接。**因此 FR-57 字面的「9 类各自产生准备单」实际落到 7 张**：轮换三段共用一张，执行侧三类共享 `execution_run` 这一聚合对象但各自独立成单。 |
| **暂存发生在审批之前** | **保留现状**（受理时暂存 → 异步验证 → 审批 → 激活） | 新凭据必须活到激活那一刻，而调用方只在受理时给过一次；能持久化它的地方只有 Secret Provider —— 暂存就是那次持久化。**代价要说清**：这一处不满足「Commit 之前零外部副作用」，受管的是**激活**那一步。验证仍然在审批之前（`WAITING_APPROVAL` 只能由验证成功的路径进入）。 |
| **凭据销毁改为双人审批** | **按 PDP 收紧**：数据审批人 + 安全审批人各一名 | 工具清单里 `secret.destroy` 的效果是 `DESTRUCTIVE`，FR-42 要求破坏性写入双人审批。此前只有单安全审批槽，属于与自己的清单自相矛盾。相关用例已改为两次具名审批（`HarnessDecisionRequest` 新增可选 `role`）。 |
| **提交由谁发起** | 副作用类：**审批齐备即提交**（`decide_for` 内部提交）；执行链路：**申请人单独 Commit**（API-07 无入参） | 前者保留已验收的「批完即执行」交互；后者是 SPEC 明确的三阶段。两者都由服务端强制：执行链路的 `commit()` 要求 `WRITABLE` 项目角色，审批人（`APPROVER`）提交会被 403 挡住。 |

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

### 本轮新增的落点（新会话按这个顺序读一遍就够）

| 文件 | 作用 |
|---|---|
| `application/harness/side_effects.py` | 副作用协议：`prepare` / `decide` / `commit` / `claim_execution` / `finish_execution` / `verify_grant` / `abandon`，以及能力令牌的登记与原子消费 |
| `application/harness/side_effect_targets.py` | 九类操作「动什么」的**唯一**推导入口 + 操作到聚合对象的映射 |
| `application/side_effect_runtime.py` | 执行侧三道闸（`begin_execution`）、执行器接口（`DorisMaintenancePort`）、执行侧三类的执行器 |
| `side_effect_tasks.py` | 通用副作用 Worker（三段式事务，见文件头） |
| `application/executions.py` / `api/routes/executions.py` | 执行查询、受管取消、API-05~08、受管交付 |
| `application/execution_delivery.py` | T-40：把执行结果落成受管文件资产（报告载荷的构造与脱敏） |
| `api/routes/{approvals,schedules,side_effects}.py` | 版本审批、调度配置、通用副作用查询面 |
| `tests/test_side_effect_protocol.py` / `tests/test_execution_api.py` | 这些的验收（21 + 18 条） |

### 第一步：M6 的第一件事，把「状态推进」补上（T-43 的一半）

`execution_runs` 现在**没有自动推进**：提交之后就没人把引擎的进行状态写回来，
`CANCELLING` 也没有人确认引擎真的停了。这一条同时是三个东西的依赖：

- SPEC T-38 的「状态未知先对账」；
- 取消语义的诚实收尾（`CANCELLING` → `CANCELLED`）；
- **T-40 的自动交付**：交付要求执行已跑出结论，而现在没有任何东西会把运行推到终态。

所以先做一个对账扫描（Beat 周期任务 + `SeaTunnelExecutionEngine.status` 投影回写），
再在它确认终态后触发交付。**交付服务本身已经是幂等的**，扫描可以放心重跑：
`deliver_report` 命中已有的交付物会直接返回那一份。

### 第二步：M6 余下部分（T-41 ~ T-46）

T-45 会填上 `DorisMaintenancePort` 的真实实现与 `rollbacks` / `cleanups` 路由。

### 收尾要做的（每轮结束时）

- **控制面库需要重置**：现在里面有前几轮烟测留下的 213 个项目（见 §5）。
  清库需要 `DROP SCHEMA public CASCADE` 这类操作，**被权限拦截过，没有执行** ——
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
| M5 | `application/harness/{preparation,approval,commit,engine,placeholders}.py`、`execution_tasks.py`、`beat_tasks.py`、`infrastructure/seatunnel_execution.py`；`application/harness/{side_effects,side_effect_targets}.py`、`application/{side_effect_runtime,executions,execution_delivery}.py`、`side_effect_tasks.py`，以及 `api/routes/{executions,approvals,schedules,side_effects}.py` |

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
> 因为当时那批 HTTP 端点还没写。HTTP 端点现已补齐（API-05~08），但**那次端到端烟测没有重跑** ——
> 接口层目前的证据是 `tests/test_execution_api.py`（14 条，走 ASGI，用假引擎与拦下的投递）。
> **「走 HTTP 的真引擎端到端」这一条还没有人跑过，别把它写成已验证。**
> 烟测脚本当时放在仓库外的临时目录，没入库。

---

## 5. 环境状态（本机，2026-10-16）

| 项 | 状态 |
|---|---|
| Git | `main` → `git@github.com:jiaiyi/data-integration-platform.git`，SSH 可直接 push（`gh` 未安装） |
| Docker 镜像 | 基础镜像 + E2E 扩展镜像（Doris FE/BE、SeaTunnel）均已加载 |
| 容器 | 当前 9 个在跑（本项目 8 个 + 一个无关的 `customer-service-mysql`）。起全栈：`docker compose -f infra/compose.yaml --profile e2e up -d --no-build --pull never` |
| 端口 | postgres `5432`、redis **`16379`**、mysql-e2e `13306`、minio `9000/9001`、Doris FE `8030/9030`、SeaTunnel `8080` |
| 控制面库 | 迁到 `20261016_0004`（head，本轮新增 `file_assets.execution_run_id`，已在真库上 `alembic upgrade` 验证，降级 SQL 也离线校验过）。**数据不干净**：有前几轮烟测留下的 **213 个项目**、4 条 `agent_runs`、1 份准备单、1 条执行记录。清库需 `DROP SCHEMA` 级别的操作，**被权限拦截过，未执行**。 |
| 制品目录 | `var/artifacts/` 下有 6 个文件（前几轮冻结的制品，已被 gitignore）；交付物另落在 `DIP_FILE_ASSET_ROOT` 下 |
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
20. **副作用表与执行表分开，但规矩同一套**。执行准备单绑的是「管道版本 + 制品字节」，
    副作用准备单绑的是「聚合对象 + 冻结的事实摘要」—— 一个凭据轮换没有制品，硬塞进
    `execution_preparations` 只会让那边多出一堆语义为空的列。两边各自的入口函数在
    `harness/preparation.py` 与 `harness/side_effects.py`，**不要为了「统一」把表合并**。
21. **副作用能力令牌的绑定项是复用执行链路的，含义要按副作用解读**：
    `artifact_sha256` = 事实摘要（副作用没有制品）、`supervision_contract_sha256` = 影响面摘要
    （副作用没有运行预算，等价约束是「影响面不得在执行期变化」）、
    `data_classification_sha256` = 数据分级摘要。写在 `side_effects.py` 的表里，别猜。
22. **「终态不可用」与「事实未漂移」必须分成两个判断**。执行侧要走的路不同：一张已提交的
    准备单当然「不能再审批」，但它正该被执行。合成一个判断会让**所有已提交的副作用在执行时
    被自己的终态挡住**（写过一次，回归用例 `test_execution_happens_once_and_consumes_the_lease`
    盯着这条）。执行侧也不查准备单 TTL —— 那一刻管用的是能力令牌自己的有效期。
23. **执行租约必须在验签之前取**。反过来先验签再抢租约的话，两个 Worker 会同时通过验签、
    同时动手，租约就白设了。同理，租约冲突（`SideEffectLeaseBusy`）**不是失败**，
    调用方应当安全延迟重试，不要落 `FAILED`。
24. **事实摘要只放不可变字段**。往里面塞 `status`、`engine_status` 这类会变的列，会让
    执行前那次重算比对把它们判成「事实被改过」，于是每一张单子都在执行时作废。
    要动的对象的**标识**进摘要，它的**状态**不进。
25. **取消的理由写在执行记录上（`cancel_reason`），不进准备单事实**。理由是「发起时的意图」，
    而准备单冻结的是「要动哪个作业」；自由文本进事实只会给指纹多一个漂移源。
    `cancel_requested_at` 记的是**请求送达引擎**的时刻，不是申请的时刻 —— 申请可能还压在审批里。
26. **路由判「要不要投递执行」看的是响应快照里的状态，不是请求里的意图**。
    多槽位审批里投一票不等于批准（`snapshot.status is APPROVED` 才算），
    看 `request.decision is APPROVE` 会在凭据销毁这类双人审批上提前投递。
27. **pydantic 默认静默丢弃未知字段**。所以 FR-55 那种「客户端不许提供事实」的入参必须写
    `model_config = ConfigDict(extra="forbid")` —— 否则客户端塞了 `risk_level` 会拿到一个
    成功的 201，它以为生效了，平台根本没看。
28. **要发多次 HTTP 请求的测试，夹具数据必须真提交**。`session` 夹具只 flush 不 commit，
    而每个请求结束时应用层的事务会结束 —— 没提交的事实下一次请求就读不到了。这类模块
    统一用 `session_factory` + 显式 `async with session.begin()`（见 `test_execution_api.py`）。
29. **交付物要能「按执行反查」并且「重复交付只留一份」**，靠文件名约定两条都做不到：
    名字可以被改、也可以重名。所以 `file_assets` 加了 `execution_run_id`（可空）
    与 `(execution_run_id, name)` 唯一约束，交付服务命中已有那份就直接返回。
    **执行记录被删时 `SET NULL` 而不是 CASCADE** —— 交付物是执行结果的留档，
    删记录不该让它一起消失。
30. **脱敏只擦字符串值，不动键名**。整段 JSON 文本丢给 `scrub_secrets` 会被
    `\b(secret|token|api_key)\b\s*[=:]` 这类关键词正则命中，把 `"api_key": "..."` 改写成
    `api_key=***` —— 产出一份**不是 JSON 的「JSON」**。交付载荷因此走
    `execution_delivery.scrub_report` 递归擦值；报告将来长出新的诊断字段也不会漏。
31. **报告用排序键 + 固定缩进的 JSON**：既人能读，又让同一份事实重复交付得到同一个
    内容摘要。摘要可复现是审计物该有的性质，紧凑单行虽然也能复现，但没人读得下去。

---

## 7. 验证的分工（别把跳过当通过）

| 手段 | 覆盖什么 | 不覆盖什么 |
|---|---|---|
| 单元 / 契约测试（742 条） | 状态机、门禁、PDP、分类、签名、重放、Outbox、账本、脱敏、格式推断、SQL 生成、身份与职责分离、九类副作用的受管入口与执行租约、受管交付 | 任何真实外部系统 |
| `pytest -m integration`（28 条） | 真实 PostgreSQL / MySQL / MinIO / Doris / SeaTunnel 提交与状态；未起服务时**整组跳过** | 需要 Docker Hub 镜像的数据源、真实 Vault、真实 LLM |
| 服务层端到端烟测 | 业务事实的完整推进（生成、三阶段协议、Outbox → Worker → 引擎） | 同上；**且它绕过 HTTP 层**，接口契约由 API 测试单独覆盖 |

**演示时最有说服力的三条命令**：

```bash
uv run pytest -m integration                          # 真实引擎的提交与状态投影
uv run pytest tests/test_side_effect_protocol.py -q   # 九类操作各自产生准备单/审批/能力记录
uv run pytest tests/test_tool_broker.py -q            # 「无绕过路径」的证据是执行器调用次数为 0
```
