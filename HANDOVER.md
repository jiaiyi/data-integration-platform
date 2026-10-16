# 交接文档

> 用途：跨会话延续本项目开发。**先读本文件，再读 `README.md`（进度表 + 本机联调坑）
> 与 `specs/SPEC.md §10`（任务分解）。**
> 最后更新：2026-10-16，对应提交 `e37a6bd`。

---

## 1. 一句话现状

M0 / M1 / M2 **已完成并真实验证**；M3 的任务（T-18 ~ T-23）**全部完成**
（其中 T-21 引擎检查已对真实 SeaTunnel 2.3.13 跑通），
只差把生成链路的 API 与 Worker 接起来 —— 见 §3 第四步。

接手时先确认仓库是好的：

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest                    # 期望 440 passed
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

### 第四步：把生成链路接起来

`pipelines` / `agent_runs` 路由目前是空壳（只有 `APIRouter` 对象，没有端点）。
需要：创建版本 → 发起生成 → 澄清回答 → 设计审查视图 → 门禁结果 → 冻结 → 提交审批。
Celery 任务参照 `connection_tasks.py` 的模式（`run_in_transaction` + `dispatch`）。

### 需要用户提供的东西

**LLM 接入信息尚未提供**：`base_url` / `model` / `api_key`。写进 `.env`（已被 `.gitignore` 排除）。
拿到之后 `DirectGenerationAdapter` 就能真实跑通；在此之前只能用剧本化假后端测试状态机。

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
