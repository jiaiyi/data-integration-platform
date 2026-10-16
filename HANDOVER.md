# 交接文档

> 用途：跨会话延续本项目开发。**先读本文件，再读 `README.md`（进度表 + 本机联调坑）
> 与 `specs/SPEC.md §10`（任务分解）。**
> 最后更新：2026-10-16（M5 进行中：三阶段协议与执行链路已完成，剩 T-36 / T-40 与执行 API）。

---

## 1. 一句话现状

M0 ~ M4 **全部完成**。M3 的生成链路已用**真实 DeepSeek + 真实 SeaTunnel 2.3.13** 端到端跑通；
M4 的 Harness 安全内核已交内核；
**M5 已完成三阶段协议与执行链路**（T-33 Prepare / T-34 Approve / T-35 Commit /
T-37 Worker 验签 / T-38 SeaTunnel 对接 / T-39 Beat），
并已用真实引擎跑通 `prepare → 审批 → commit → outbox → worker → 引擎 FINISHED`。
**M5 未完成的部分见 §2 与 §3 第六步**（T-36 通用副作用框架、T-40 交付、执行 API）。

接手时先确认仓库是好的：

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest                    # 期望 643 passed
docker compose -f infra/compose.yaml --profile e2e ps   # 期望 8 个服务在跑
```

若 `pytest` 里有整组跳过，那是 `tests/integration/` —— 它需要 Compose 起的真实服务，
**跳过不等于通过**，别把跳过当证据。

---

## 2. 需求满足度对照（**最重要的一节**）

防止把「没做」当成「做了」。以下是刻意未满足或只做了一半的需求。

| 需求 | 状态 | 说明 |
|------|------|------|
| **FR-57 / FR-23**「所有副作用走同一受管入口」 | **只做了一半** | 凭据四类操作已有「申请→具名审批→执行」的数据结构与状态机，审批人还必须持 `SECURITY_APPROVER` 槽位。但把它们接到 Harness 的 **Capability / 事务 Outbox** 上属 M5 的 T-36。**在此之前不得声称 FR-57 已满足。** |
| **FR-35 / T-21** 真实引擎检查 | **已做，但能力边界要说清** | 见 §3 第一步。**不要声称等价于 SPEC 写的「引擎 `--check`」** —— 那个机制在 2.3.13 上不存在。本层做的是「引擎可用性 + 引擎自己的配置解析器判文档结构」，**不校验连接器选项**（那一层只在提交时的作业解析里发生）。 |
| **FR-28**「生成后端可切换」 | **切换点存在，第二后端未接入** | `create_generation_adapter` 遇到 `seatunnel-cli` 会**明确报错**，不静默降级。这是刻意的（与探查器注册表同款姿态）。 |
| **FR-33** 逐行 LLM Transform | **只做门禁侧** | 默认严格关闭、开启需独立密钥与出口白名单，这两条已落配置与门禁；能力本身未实现（按决策 Q7「按参考实现交付」，等 M3 后半段评估）。 |
| **T-15 的 Vault 真实验证** | **用假服务代替** | 本机拉不到 `hashicorp/vault` 镜像，集成测试用一个实现 KV v2 语义的本地 HTTP 服务代替（`tests/integration/test_secret_lifecycle_live.py`）。**Vault 客户端代码已被真实请求路径验证，但没对真实 Vault 验证过。** |
| ClickHouse / Oracle / Hive / HDFS | **只有契约测试** | 这四类镜像拉不到，真库验证做不了。由 `tests/test_probe_contracts.py` 覆盖（无无界 COUNT、有受限行数、拒绝标识符注入、注册表覆盖全类型）。**不要写成「已真实验证」。** |
| **「提交审批」只做到创建审批单** | **只做了一半** | 版本状态迁到 `WAITING_APPROVAL` 并落一条绑定 `artifact_sha256` 的 `ApprovalRequest`。**审批决策（通过/驳回）、双人职责分离、能力令牌与执行准备单都还没做**，属 M5 的 T-33 ~ T-36。 |
| **`schedules` 路由仍是空壳** | **未做** | T-23 的调度**用例**（`application/schedules.py`）已完成并经真实引擎无关的纯计算测试，但 SPEC §6.1 里那 3 个调度端点还没接 —— 本轮的接线范围只到生成链路。 |
| 调度扫描与 Beat | **未做** | `PipelineScheduleService.advance` 是扫描要调的那一步，但周期扫描任务属 T-39（M5）。压测前不会自己触发。 |
| **FR-49 策略包「签名」** | **只做到版本化** | 规则集在代码里（`application/harness/pdp.py`），决策记录 `policy_bundle_version` 与全部命中规则。**自签自验证明不了什么**，所以没做签名；要能热更与回滚，得先把规则集外置成清单文件再对文件签名。 |
| **`harness_public_keys` 表** | **改用配置** | SPEC §8.1 写的是 Worker 挂一张公钥映射表，实际做成配置项 `DIP_HARNESS_SIGNING_PUBLIC_KEYS`（`key_id:base64url` 逗号分隔，支持轮换期多把）。理由是 M4 里没有这张表的写入方与运维端点；代价是加一把钥匙要重新部署 —— 对信任根而言这个代价可以接受，但这是**与 SPEC 的差异**，别以为表已经建了。 |
| **证据签名与能力签名共用一把密钥** | **M5 必须拆开** | `ledger.append` 只要求一个 `SignerPort`，当前沿用与能力令牌同一把。等 Worker 开始产生执行证据（M5），**必须为证据单独配一把密钥**：否则为了让它签证据，就得把签发能力的私钥也交给它，「Worker 在结构上无法自行签发能力」这条边界会直接消失。 |
| **Harness 内核尚未接进副作用链路** | **执行链路已接上，其余九类未接** | 生成与执行的链路已经走 Broker/Capability/Outbox/Ledger（T-35、T-37）。但**九类受管副作用**（取消、回滚、清理、凭据迁移/轮换/回退/销毁）仍然走 M2 各自的既有路径，**没有**统一到 T-36 的入口。FR-57/FR-23 到 T-36 完成才算满足。 |
| **T-40 交付与受管文件输出** | **未做** | 执行结果仍然只是引擎侧的作业状态；把结果交付成受管文件对象还没做。 |
| **执行相关的 API 端点未接** | **未做** | `engineers`/`executions` 路由仍是空壳：准备单的创建/查询、审批决策、Commit、执行查询（SPEC API-05 ~ API-08）都还没有 HTTP 入口 —— 目前只能由服务层调用（§3 第六步记录了真实烟测怎么跑的）。 |
| **占位符名字是按约定导出的** | **本轮新定的约定** | HOCON 里的凭据占位符固定写成 `<角色>_<连接器>_<字段>`（如 `${SOURCE_MYSQL_PASSWORD}`），生成阶段把可用名字喂给模型，执行阶段按同一份清单替换，**表外的名字一律拒提交**。规格里没定义这个映射，这是本轮定的（见 §3 第六步）。 |
| M7 控制台（T-47~T-53） | **只有骨架** | 前端是五视图骨架 + 原型设计令牌，页面逻辑未做。M2/M3 的后端接口目前**没有前端在用**。 |

### 一处刻意的硬失败边界（不是 bug）

远端文件（S3/MinIO、HDFS）只做**前缀范围采样**（默认 8 MiB）。Parquet / ORC / Excel / PDF 的
结构信息在**文件尾部**，超过上限时**直接报错**而不是「读一半再推断」——后者会产出假结构。
文本类格式（CSV/JSON/JSONL/XML/Markdown）走前缀读取，此时**行数留空**而不是拿部分字节算出的数冒充。
统一策略在 `infrastructure/remote_probe.py::plan_remote_read`。

---

## 3. 下一步该做什么（按顺序）

### 第一步：T-21 真实引擎检查 —— 已完成（`fb633fa` 之后）

探明后的**实测结论**（这些是排查出来的，别再重新踩）：

- REST 端点挂在**根**下，不是 `/hazelcast/rest/maps/*`：`/overview`、`/running-jobs`、
  `/finished-jobs`、`/pending-jobs`、`/job-info`、`/submit-job`、`/encrypt-config` 等。
- CLI 的 `--check` **对作业配置是空操作**（语法损坏、插件不存在的配置都返回 0）——
  它校验的是**集群配置**。所以 SPEC §10 写的「引擎 `--check`」在 2.3.13 上不成立。
- REST **没有 dry-run**：`/submit-job` 接受 HOCON 文本但会真的起作业，不能用在冻结之前。
- `/encrypt-config` 用引擎自己的配置解析器解析，**无副作用**，但只吃 **JSON 配置文档**
  （HOCON 文本一律回 `Invalid JSON format in request body.`），且 `source`/`transform`/`sink`
  必须是**插件对象数组**（传对象报 `LinkedHashMap cannot be cast to ArrayList`）。

落地形态：`application/engine_check.py`（`EngineCheckPort` + `gate_runner_with_engine` /
`build_gate_runner`）+ `infrastructure/seatunnel_engine_check.py`（`SeaTunnelEngineCheck`）。
候选 HOCON 经 `pyhocon` 转成引擎要的 JSON 文档再送检。**按 `MetadataProbePort` 的模式只做了
Port + Adapter，没有做注册表** —— 执行引擎只有 SeaTunnel 一家，没有切换需求，注册表是空抽象。

**边界（不许含糊）**：这一层判的是配置**文档结构**（缺 source/sink、source 为空这类，
由引擎自己报「Miss &lt;Source&gt; config!」），**不校验连接器选项**；选项级校验只发生在
提交时的作业解析阶段。确定性门禁不过时**不再外呼引擎**（候选已被拒，省一次外部调用，
也免得修复提示被引擎结论挤掉）。

### 第二步：T-22 不可变版本与 Artifact —— 已完成

`application/versions.py::PipelineVersionService`：`create`（含续用同名管道的版本序列）、
`revise`（V+1，挂 `parent_version_id`，**不触碰父版本**）、`freeze`、`artifacts_of`、`read_artifact`。

冻结落三份制品：`HOCON`（原样字节）、`ETL_PLAN`、`REVIEW_VIEW`（plan 的纯投影）。
`version.artifact_sha256 = canonical_sha256(candidate.canonical_payload())`，
即 plan + hocon 的规范摘要 —— 审查视图是二者的确定性投影，所以摘要覆盖它而不用单独入摘要。

关键约束（松掉任何一条，US-09「审批人看到的就是将要执行的内容」就只是口号）：
- `freeze` **只接受 `GENERATED`**，由状态机迁到 `VALIDATED`；DRAFT 直接冻结会被
  `InvalidStateTransition` 拒（跳步），已冻结的再冻结会被 `DomainError` 拒（不可重入）。
- **落盘不是不可变性的执行边界**：真正挡住改写的是上面的状态检查与
  `pipeline_artifacts(version_id, artifact_type)` 的唯一约束。文件系统只负责存字节。
- 制品路径 `<artifact_root>/<project_id>/<version_id>/<TYPE>.<ext>`。

### 第三步：T-23 调度策略 —— 已完成

`application/schedules.py`：纯计算部分（`ScheduleSpec` / `plan_tick` / `next_after` /
`previous_before` / `initial_window`）与持久化（`PipelineScheduleService.configure` /
`set_enabled` / `advance`）分开，判定可以脱离数据库被穷举测试。

**踩过的坑：croniter 默认把秒放在最后。** PRD 写的是 `0 0 2 * * *`（秒在前），
不显式 `second_at_beginning=True` 的话它会被读成「每月 2 日 0 点」——任务照跑、时间全错、
不报任何错。`croniter.is_valid()` **也要**显式传这个参数，否则 `0 30 3 * * *` 会被判非法。

**水位窗口的不变量**：窗口永远是「即将发生的那次触发」要读的 `[起点, 触发时刻)`，
起点 = 上一次触发时刻，所以 `watermark_window_end == next_run_at` 恒成立。
`SKIP` 跳过的区间**不推进起点**，会并进下一次触发的窗口，不会凭空消失。

**失火只补一次**：`FIRE_ONCE` 不是「错过几个补几次」（停机一周逐条重放会把上游打垮），
补跑的水位窗口覆盖整个错过的区间，水位增量模式下因此不丢数据。
配置调度要求版本**已冻结**（`artifact_sha256` 非空）；「已批准才能启用」这条属 FR-65，
留给 M5 的扫描与执行阶段，别在这一层假装已经做到。

### 第四步：把生成链路接起来 —— 已完成，并已真实跑通

端点（SPEC §6.2）：`POST /pipelines`（API-01）、`POST /pipeline-versions/{id}/revisions`、
`GET /pipelines/versions/{id}/design`（API-04）、`POST /pipeline-versions/{id}/approval-requests`、
`POST /pipeline-versions/{id}/agent-runs`（API-02）、`POST /agent-runs/{id}/answers`（API-03）、
`GET /agent-runs/{id}`。

Worker 侧：`application/generation_runs.py`（受理 / 受管上下文 / 结论落地）+
`generation_tasks.py`（Celery 任务）。三段式：**短事务取上下文 → 不占事务跑状态机 →
短事务落结论**。

两个刻意的设计决定：

- **冻结由 Worker 做，不单开端点。** FR-35 要求「通过门禁与真实引擎检查后才允许冻结」；
  放在状态机出边之后这条顺序是结构上成立的，开成两个入口就得靠调用方记得先跑门禁。
- **澄清答案随任务载荷走，不落业务库。** 生成过程的中间状态归 Checkpoint 库（`dip_checkpoint`），
  业务库只记结论。

真实端到端验证记录（2026-10-16，MySQL `source_db.orders` → Doris `ods.order_logs`）：

```
create version → DRAFT
start agent run → QUEUED →（Worker）RUNNING → WAITING_APPROVAL
  candidates=1 repairs=0，generation_phase=WAIT_FOR_REVIEW
design view: VALIDATED，artifact_sha256=67a5bb25…（HOCON 从冻结制品读回）
submit approval → 201 PENDING，绑定同一摘要；重复提交被拒
revision → v2，parent_version_id 指向 v1，v1 状态与摘要未动
```

### 第五步：M4 Harness 安全内核 —— 已完成（内核，未接线）

`application/harness/` 下七个模块，每个对应一个任务：

| 模块 | 任务 | 要点 |
|---|---|---|
| `tools.py` | T-24 | 清单目录；未注册 / 版本不匹配 / 输入越界三拒。输入 Schema 由 pydantic 模型派生，一份来源两处使用。构造期查「补偿工具必须已注册」「同名效果必须一致」。 |
| `pdp.py` | T-25 | 纯计算规则表，先命中先裁决但**全部命中都留痕**；没命中 = 拒绝。 |
| `classification.py` | T-26 | 字段级分类 + 血缘传播（只升不降）+ 未解析清单；报告摘要对字段顺序不敏感。 |
| `signing.py` + `infrastructure/ed25519_signing.py` | T-28 | 端口与 Ed25519 实现；**公钥映射多把**以支持轮换；空映射 = 一切验签失败。 |
| `grants.py` | T-29 | 原子消费（一条带条件的 UPDATE）+ `token_sha256` 唯一。 |
| `broker.py` | T-27 | 唯一出口，执行前六项重校；**证据形式是「执行器调用次数为 0」**。 |
| `outbox.py` | T-30 | 与本地事实同事务入队；租约 + 逐行条件更新领取。 |
| `ledger.py` | T-31 | 哈希链 + 每事件签名；异常**分类**报出（缺号/断链/载荷被改/签名无效/链尾不符）。 |
| `failclosed.py` | T-32 | 五项信任输入；**「没提供」记作「验不了」**，只读不受约束。 |

### 第六步：M5 三阶段协议与执行 —— 进行中（已完成的部分）

| 模块 | 任务 | 要点 |
|---|---|---|
| `application/harness/preparation.py` | T-33 | Prepare 推导输入/资源/分级/预算/回滚方案；**零外部副作用**（测试把 `socket.connect` 换成会炸的实现）。连接由服务端按计划里的库表**查实**，不采信模型写的 `connection_id`。 |
| `application/harness/approval.py` | T-34 | 具名审批 + 职责槽校验；**重算指纹**，事实变了就把准备单作废（旧审批一并失效）；SOD-01/02 都在这里。 |
| `application/harness/commit.py` | T-35 | 服务端重建事实 → 签发能力 → 经 Broker **同事务**消费令牌 + 建 Run + 写 Outbox；风险等级与准备单冻结值比对，对不上就拒。 |
| `execution_tasks.py` | T-37 | **验签在读 Secret 与碰引擎之前**；测试断言令牌不成立时假引擎与假凭据存储的调用次数都是 0。 |
| `infrastructure/seatunnel_execution.py` | T-38 | 实测出来的 REST 契约（见下面的坑）。 |
| `beat_tasks.py` | T-39 | 心跳 + 凭据告警扫描 + 调度扫描 + Outbox 扫描。调度到点**走受管入口**生成待审批准备单，不直接执行。 |
| `application/harness/placeholders.py` | —— | 占位符名字约定与物化；见上表那条边界。 |

**真实端到端已验证**（服务层直调，2026-10-16）：

```
prepare → WAITING_APPROVAL(P2, [DATA_APPROVER], 1)
approve → READY
commit  → run QUEUED + 准备单 COMMITTED + Outbox PENDING
outbox scan → claim 1 条
worker  → 验签通过 → 物化 → 提交 → SUBMITTED(job 1163409713448222721)
engine  → jobStatus FINISHED（起止时间来自引擎自述）
```

**这一步踩到的坑（都写进代码注释了）**：

- `/submit-job` 的 `format` 是**查询参数**、请求体是 **HOCON 原文**。照文档塞进 JSON 字段
  会落到默认的 JSON 分支，那条分支在 2.3.13 上引擎自己会 NPE（回 500 且消息为空）。
- HOCON 一行里连写多个字段（`a = 1 b = 2`）会解析失败 —— 必须用换行或逗号分隔。
- 没有「按 jobId 查状态」的可用接口（`/job-info` 只允许 GET 又要读请求体）；
  状态得去 `/running-jobs` `/pending-jobs` `/finished-jobs` 三个列表里找，找不到返回 `None` 让调用方对账。
- `CapabilityMismatch` **刻意不继承 `DomainError`**，Worker 里必须显式接住，否则它会穿透成一个没有结论的异常。
- 事实推导**只能有一条路径**（`prepared_facts_for`）：Prepare/审批前校验/Commit 各推一遍的话，
  任何一处漏了某个输入（Commit 忘了带连接）都会让指纹漂移，而漂移看起来像「事实被改过」。

### 需要用户提供的东西

**LLM 接入信息已提供并写进 `.env`**（`.env` 被 `.gitignore` 排除，不入仓库）：
`DIP_LLM_BASE_URL` / `DIP_LLM_API_KEY` / `DIP_LLM_MODEL`。

⚠️ **注意协议差别**：给的是 `https://api.deepseek.com/anthropic`（Anthropic Messages 协议），
而本项目的 `DirectGenerationAdapter` 走 **OpenAI 兼容**的 `{base}/chat/completions`，
所以 `.env` 里写的是 `https://api.deepseek.com`（同一个 key 与模型，实测两套协议都通）。
要换成 Anthropic 协议得改 Adapter，不是改配置能解决的事。

`.env` 里的 `DIP_BOOTSTRAP_ENABLED` 用完应改回 `false`（FR-05：初始化是一次性动作）。

---

## 4. 环境状态（本机，2026-10-16）

| 项 | 状态 |
|---|---|
| Git | `main` → `git@github.com:jiaiyi/data-integration-platform.git`，SSH 可直接 push（`gh` 未安装） |
| Docker 镜像 | 基础镜像 + E2E 扩展镜像（Doris FE/BE、SeaTunnel）均已加载 |
| 容器 | `docker compose --profile e2e up -d --no-build --pull never` 可起全栈：postgres `5432`、redis **`16379`**、mysql-e2e `13306`、minio `9000/9001`、Doris FE `8030/9030`、SeaTunnel `8080` |
| 控制面库 | 已迁到 `20261016_0003`，**数据为空**（每次烟测后都会重置） |
| 注册表 | **Docker Hub 不通**，只有 `mcr.microsoft.com` 可达 → ClickHouse/Oracle/Hive/HDFS/Vault 镜像拉不到 |

**用户要求：每完成一步就 commit + push**（不要攒成一个大提交）。

---

## 5. 只在对话里说过、但必须延续的设计判断

这些是排查出来的结论，不写下来新会话会重新推导甚至推翻：

1. **枚举列长度固定为 `models.ENUM_COLUMN_LENGTH = 64`。** 不指定长度时 SQLAlchemy 按
   「建表当时最长枚举值」生成 `VARCHAR(n)`，于是**新增一个更长的枚举值会在 PostgreSQL 插入失败**
   （实测：`audit_events.event_type` 是 varchar(19)，新增值 23 字符）。
   **SQLite 不校验长度，所以只在真库上暴露。** `tests/test_schema_contract.py` 守住这条。
2. **平台主动外发一律 `trust_env=False`**（Vault / Webhook / REST 数据源 / WebHDFS / LLM）。
   走系统代理不仅会连不上内网地址，**还会把 Vault token、API Key、鉴权头交给未知中间人**。
3. **连接串一律写 `127.0.0.1` 而不是 `localhost`。** Windows 优先解析到 IPv6 `::1`，
   而 Docker 只发布 IPv4（实测 Redis 差 250 倍，且无超时的客户端会直接挂住）。
4. **Worker 必须进程级复用同一个事件循环**（`asyncio_compat`）。每个任务 `asyncio.run`
   新建循环会让连接池绑在旧循环上，报 `'NoneType' object has no attribute 'send'`。
   **Windows 上这个循环用 Selector 而不是 Proactor**：asyncpg 两种都能跑（实测），
   而 psycopg（LangGraph 的 Checkpoint 库）明确拒绝 Proactor —— Selector 是唯一交集。
5b. **在协程里不要用 `run_in_transaction`**：它内部是 `run_async` → `run_until_complete`，
   协程已经在那条循环上跑着，再进去就是「事件循环已在运行」。协程内部直接 `async with
   SessionFactory()`。
5c. **未预期的异常也要先落事实**。生成任务的 `_invoke` 一度只捕获 `DomainError`，
   结果 Checkpointer 的实现异常直接抛出、事务回滚，运行**永久停在 RUNNING**。
   与探查那边「失败也要先记录终态」是同一条要求。
5d. **分级序位只有一份**（`domain/harness.SENSITIVITY_ORDER`）。PDP 判风险与分类报告算总级
   都读它。之前两处各写了一遍，`UNKNOWN` 在一处按「受限」、在另一处按「秘密」——
   这类重复不会立刻报错，只会在某个字段上悄悄给出不同结论。
   `UNKNOWN` 刻意排在「受限」与「秘密」之间：比已知受限更该被盯着，但等同秘密会让
   任何未分类数据都出不了门，系统直接不可用。
5e. **SQLAlchemy 的 SQL 侧自增表达式不会刷新会话里的对象**。Outbox 领用用
   `attempts = attempts + 1` 后，会话里的对象仍读旧值，导致重试上限永远触发不了。
   再查一次时加 `populate_existing=True`。
5f. **提交/回滚之后读 ORM 属性会触发同步惰性加载**，在 async 会话里直接抛
   `MissingGreenlet`。要用的主键在提交前先取出来。
5. **`.delay()` 不能在 async 路由里直接调用**（同步阻塞会卡住整个事件循环），
   统一走 `celery_app.dispatch` 卸载到线程。
6. **凭据操作不做自动重试**（可能已部分生效，重试会放大问题）；
   **告警投递可以重试**（接收端按 `delivery_id` 幂等）。两者刻意相反。
7. **`session.begin()` 之前不能先查库**：`AsyncSession` 首次查询会自动开启事务，
   之后再 `begin()` 抛 `A transaction is already begun`（踩过一次，已有回归测试）。
8. **引擎可搬运 ≠ 可登记**：`REST_API` 满足 FR-103（登记 + 探查），但 SeaTunnel 没有对应
   Connector，**不能作为管道端点**——门禁在 `ENGINE_MOVABLE_CONNECTORS` 里拦住它。
9. **不要写「占位路由」或「返回 501 的假端点」**。未实现的模块只留空的 `APIRouter`
   并挂载；伪造端点会在下个里程碑全部删掉，是纯噪音。
10. **`as_utc()` 统一时间口径**：SQLite 读回的 `DateTime(timezone=True)` 不带 tzinfo，
    直接与 aware 时间比较会抛 `TypeError`。领域比较一律先归一化。

---

## 6. 验证的分工（别把跳过当通过）

| 手段 | 覆盖什么 | 不覆盖什么 |
|---|---|---|
| 单元 / 契约测试 | 状态机、门禁、脱敏、格式推断、SQL 生成、身份与职责分离 | 任何真实外部系统 |
| `pytest -m integration` | 真实 PostgreSQL / MySQL / MinIO / Doris / SeaTunnel、真实 HTTP 往返 | 需要 Docker Hub 镜像的数据源、真实 Vault、真实 LLM |
| 端到端烟测 | API → Redis → Worker → 外部系统的完整链路 | 同上 |

**每轮收尾时**：重置控制面库、删掉 `.env.smoke` 之类临时文件、停掉临时的 API/Worker 进程。
