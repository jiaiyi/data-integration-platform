"""生成 生产级 ETL-Agent 数据集成平台 原型图（.pen）。

页面依据：PRD v1.2 §7.1~§7.5（五类页面对应五类工作任务）
输出：prototype.pen

用法：python generate_prototype.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from penlib import (
    AMBER,
    AMBER_SOFT,
    BORDER,
    CARD,
    COL_STEP,
    GREEN,
    GREEN_SOFT,
    MUTED,
    PRIMARY,
    PRIMARY_SOFT,
    PURPLE,
    PURPLE_SOFT,
    RED,
    RED_SOFT,
    ROW_STEP,
    SUB,
    TEAL,
    TEAL_SOFT,
    TEXT,
    H,
    I,
    R,
    T,
    V,
    badge,
    btn,
    card,
    field,
    hr,
    kbrow,
    kpi,
    meter,
    page,
    table,
    vbar,
)

W = 1150  # 内容区可用宽度（1440 - 侧栏 228 - 左右 padding）


# ============================================================ 1. 总览工作台
def board_overview(x, y):
    kpis = H(
        [
            kpi("数据连接", "12", "3 个待探查", AMBER),
            kpi("Pipeline", "28", "5 个已批准", GREEN),
            kpi("待审批门禁", "3", "需 2 人独立审批", RED),
            kpi("运行成功率", "96.4%", "+1.2% 环比", GREEN),
        ],
        gap=14,
        w="fill_container",
        h=96,
    )

    approvals = card(
        "待办审批门禁",
        [
            H(
                [
                    badge("数据维度", PRIMARY, PRIMARY_SOFT),
                    T("PV-1042 · 订单宽表增量同步", size=12, weight="600", color=TEXT, lh=1.3),
                    T("2h 前", size=10.5, color=MUTED, lh=1.2),
                ],
                gap=8,
                w="fill_container",
            ),
            H(
                [T("等待 数据审批人 审批 · Artifact 3f2a…9c41", size=11, color=MUTED, lh=1.3)],
                w="fill_container",
            ),
            hr("#F1F5F9"),
            H(
                [
                    badge("安全维度", PURPLE, PURPLE_SOFT),
                    T("PV-1043 · 用户表脱敏同步", size=12, weight="600", color=TEXT, lh=1.3),
                    T("4h 前", size=10.5, color=MUTED, lh=1.2),
                ],
                gap=8,
                w="fill_container",
            ),
            H(
                [T("等待 安全审批人 审批 · 风险等级 P1", size=11, color=MUTED, lh=1.3)],
                w="fill_container",
            ),
            hr("#F1F5F9"),
            H(
                [
                    badge("数据维度", PRIMARY, PRIMARY_SOFT),
                    T("PV-1044 · 支付流水同步", size=12, weight="600", color=TEXT, lh=1.3),
                    T("6h 前", size=10.5, color=MUTED, lh=1.2),
                ],
                gap=8,
                w="fill_container",
            ),
            H(
                [T("准备单 24h 后过期 · 逾期需重新 Prepare", size=11, color=AMBER, lh=1.3)],
                w="fill_container",
            ),
        ],
        subtitle="按提交时间排序，区分数据维度与安全维度",
        pad=14,
        gap=9,
    )
    approvals["width"] = "fill_container"

    dist = card(
        "核心资源分布",
        [
            H(
                [
                    vbar("MySQL", 34, PRIMARY),
                    vbar("Doris", 26, TEAL),
                    vbar("文件", 18, PURPLE),
                    vbar("PG", 12, AMBER),
                    vbar("其他", 10, MUTED),
                ],
                gap=10,
                justify="space_between",
                w="fill_container",
                h=126,
                align="end",
            ),
            hr("#F1F5F9"),
            kbrow("源端 / 目标端", "8 / 4", TEXT),
            kbrow("受管文件", "6 个 · 共 42 MiB", TEXT),
            kbrow("凭据到期预警", "1 条", AMBER),
        ],
        subtitle="按数据源类型统计连接数",
        pad=14,
        gap=11,
    )
    dist["width"] = "fill_container"

    recent = table(
        ["Pipeline 版本", "状态", "读取行数", "质量结论", "耗时", "触发方式"],
        [
            [
                "PV-1041 · 订单宽表",
                badge("已发布", GREEN, GREEN_SOFT),
                "3,540,000",
                badge("通过", GREEN, GREEN_SOFT),
                "412s",
                "调度",
            ],
            [
                "PV-1038 · 物流投诉",
                badge("运行中", PRIMARY, PRIMARY_SOFT),
                "1,182,400",
                "—",
                "128s",
                "手动",
            ],
            [
                "PV-1035 · 用户画像",
                badge("已回滚", AMBER, AMBER_SOFT),
                "860,200",
                badge("拒绝率超限", RED, RED_SOFT),
                "96s",
                "调度",
            ],
            [
                "PV-1030 · 商品快照",
                badge("已失败", RED, RED_SOFT),
                "0",
                badge("引擎异常", RED, RED_SOFT),
                "12s",
                "手动",
            ],
        ],
        widths=[260, 96, 120, 118, 76, 84],
    )
    recent["width"] = "fill_container"

    body = V(
        [kpis, H([approvals, dist], gap=14, w="fill_container", align="start"), recent],
        gap=14,
        w="fill_container",
    )
    return page(
        "01 · 总览工作台",
        "page-overview",
        "overview",
        "总览工作台",
        "项目概况与待办入口 · 不提供直接提交作业的操作",
        body,
        x,
        y,
    )


# ============================================================ 2. 数据连接
def board_connections(x, y):
    toolbar = H(
        [
            H(
                [I("search", 14, MUTED), T("搜索连接、文件资产…", size=12, color=MUTED, lh=1.2)],
                gap=8,
                padding=[8, 12],
                radius=8,
                fill=CARD,
                stroke=BORDER,
                w=320,
            ),
            H([btn("上传文件", "ghost", "upload"), btn("新建连接", "primary", "plus")], gap=8),
        ],
        justify="space_between",
        w="fill_container",
    )

    conn_table = table(
        ["连接名称", "类型", "角色", "连接状态", "Profile", "最近探查", "操作"],
        [
            [
                "orders_mysql",
                badge("MYSQL", PRIMARY, PRIMARY_SOFT),
                badge("SOURCE", TEAL, TEAL_SOFT),
                badge("就绪", GREEN, GREEN_SOFT),
                badge("就绪", GREEN, GREEN_SOFT),
                "2h 前",
                T("测试 · 探查", size=11, weight="600", color=PRIMARY, lh=1.2),
            ],
            [
                "dw_doris",
                badge("DORIS", PURPLE, PURPLE_SOFT),
                badge("SINK", RED, RED_SOFT),
                badge("就绪", GREEN, GREEN_SOFT),
                badge("就绪", GREEN, GREEN_SOFT),
                "2h 前",
                T("测试 · 探查", size=11, weight="600", color=PRIMARY, lh=1.2),
            ],
            [
                "crm_ck",
                badge("CLICKHOUSE", TEAL, TEAL_SOFT),
                badge("SOURCE", TEAL, TEAL_SOFT),
                badge("未测试", MUTED, "#F1F5F9"),
                T("—", size=12, color=MUTED, lh=1.2),
                "—",
                T("测试 · 探查", size=11, weight="600", color=PRIMARY, lh=1.2),
            ],
            [
                "logs_minio",
                badge("S3/MINIO", AMBER, AMBER_SOFT),
                badge("SOURCE", TEAL, TEAL_SOFT),
                badge("就绪", GREEN, GREEN_SOFT),
                badge("就绪", GREEN, GREEN_SOFT),
                "1d 前",
                T("测试 · 探查", size=11, weight="600", color=PRIMARY, lh=1.2),
            ],
            [
                "orders_api",
                badge("REST API", PRIMARY, PRIMARY_SOFT),
                badge("SOURCE", TEAL, TEAL_SOFT),
                badge("测试中", AMBER, AMBER_SOFT),
                T("—", size=12, color=MUTED, lh=1.2),
                "—",
                T("测试 · 探查", size=11, weight="600", color=PRIMARY, lh=1.2),
            ],
        ],
        widths=[132, 104, 76, 84, 80, 74, 116],
    )
    conn_table["width"] = "fill_container"

    detail = card(
        "连接详情",
        [
            field("名称", "orders_mysql"),
            field("类型 / 角色", "MySQL · SOURCE"),
            field("主机端口", "10.20.3.11:3306"),
            field("数据库", "ecs_orders"),
            H(
                [
                    T("凭据引用", size=11.5, color=MUTED, w=104, lh=1.3),
                    H(
                        [badge("vault://etl-agent/connections/orders_mysql", SUB, "#F1F5F9")],
                        w="fill_container",
                    ),
                ],
                gap=10,
                w="fill_container",
            ),
            hr("#F1F5F9"),
            H(
                [
                    btn("测试连通性", "ghost", "plug-zap"),
                    btn("发起元数据探查", "soft", "scan-search"),
                ],
                gap=8,
            ),
            H(
                [
                    T(
                        "Secret 一律以引用形式展示，不提供明文查看入口",
                        size=10.5,
                        color=MUTED,
                        lh=1.3,
                    )
                ],
                w="fill_container",
            ),
        ],
        pad=14,
        gap=9,
    )
    detail["width"] = "fill_container"

    profile = card(
        "Profile 视图 · orders / orders_main",
        [
            table(
                ["字段名", "类型", "可空", "主键", "近似统计", "脱敏样本"],
                [
                    ["order_id", "BIGINT", "否", "PK", "3.54M 行", "1000231"],
                    ["user_id", "BIGINT", "否", "—", "空值 0.0%", "88231"],
                    ["user_email", "VARCHAR(128)", "是", "—", "空值 1.2%", "a***@ex***.com"],
                    ["user_phone", "VARCHAR(20)", "是", "—", "空值 0.4%", "138****1234"],
                    ["amount", "DECIMAL(18,2)", "否", "—", "min 0.01", "128.50"],
                ],
                widths=[118, 116, 50, 46, 88, 112],
            ),
            H(
                [
                    T(
                        "有效期至 2026-10-23 18:00 · 过期后不可用于生成",
                        size=10.5,
                        color=MUTED,
                        lh=1.3,
                    ),
                    badge("原文不落盘", GREEN, GREEN_SOFT),
                ],
                gap=10,
                justify="space_between",
                w="fill_container",
            ),
        ],
        subtitle="只读探查 · 不使用无界全表 COUNT · 样本默认脱敏",
        pad=14,
        gap=10,
    )
    profile["width"] = "fill_container"

    body = V(
        [toolbar, conn_table, H([detail, profile], gap=14, w="fill_container", align="start")],
        gap=14,
        w="fill_container",
    )
    return page(
        "02 · 数据连接",
        "page-connections",
        "connections",
        "数据连接",
        "登记连接、测试、Profile 探查、文件资产与 Secret 生命周期",
        body,
        x,
        y,
    )


# ============================================================ 3. Pipeline Studio
def board_studio(x, y):
    header = H(
        [
            H(
                [
                    badge("版本 v4", PRIMARY, PRIMARY_SOFT),
                    T("订单宽表增量同步", size=14, weight="700", color=TEXT, lh=1.3),
                    badge("已校验", GREEN, GREEN_SOFT),
                ],
                gap=10,
            ),
            H(
                [btn("对比版本", "ghost", "git-compare"), btn("冻结并提交审批", "primary", "lock")],
                gap=8,
            ),
        ],
        justify="space_between",
        w="fill_container",
    )

    # --- 左栏：需求输入 + 澄清
    req = card(
        "① 需求输入",
        [
            kbrow("源端", "orders_mysql · SOURCE"),
            kbrow("目标端", "dw_doris · SINK"),
            R("fill_container", 1, "#F1F5F9"),
            V(
                [
                    T(
                        "把订单表和用户表同步到 Doris 宽表，金额必须为正，邮箱脱敏，错误数据单独保存，每天凌晨执行。",
                        size=11.5,
                        color=TEXT,
                        lh=1.6,
                        w=228,
                    )
                ],
                padding=10,
                radius=8,
                fill="#F8FAFC",
                stroke=BORDER,
            ),
            H([btn("生成方案", "primary", "sparkles", w="fill_container")]),
        ],
        pad=14,
        gap=10,
    )
    req["width"] = "fill_container"

    clar = card(
        "澄清对话（LangGraph 中断）",
        [
            H(
                [
                    badge("Q", AMBER, AMBER_SOFT),
                    T(
                        "增量水位列未指定，请选择用于增量同步的时间列。",
                        size=11.5,
                        color=TEXT,
                        lh=1.5,
                        w=196,
                    ),
                ],
                gap=8,
                align="start",
                w="fill_container",
            ),
            H(
                [
                    I("corner-down-right", 14, PRIMARY),
                    T("updated_at", size=11.5, weight="600", color=PRIMARY, lh=1.3),
                ],
                gap=8,
            ),
            hr("#F1F5F9"),
            kbrow("thread_id", "a13f…9c41"),
            kbrow("中断状态", "WAITING_INPUT", AMBER),
            H(
                [T("补充后从同一 thread_id 恢复，不从零重跑", size=10.5, color=MUTED, lh=1.3)],
                w="fill_container",
            ),
        ],
        subtitle="缺参数不臆造，必须中断提问",
        pad=14,
        gap=10,
    )
    clar["width"] = "fill_container"

    # --- 中栏：EtlPlan + HOCON
    plan = card(
        "② EtlPlan（结构化）",
        [
            kbrow("源 → 目标", "orders_main → dw.orders_wide"),
            kbrow("主键去重", "order_id（已补齐）", GREEN),
            kbrow("转换", "金额过滤 · 邮箱脱敏 · 类型对齐"),
            kbrow("质量规则", "E_NOT_NULL / E_POSITIVE / E_EMAIL"),
            kbrow("增量水位", "updated_at（WATERMARK_INCREMENTAL）"),
            kbrow("运行预算", "500 万行 / 4 GiB / 1800s"),
        ],
        pad=14,
        gap=9,
    )
    plan["width"] = "fill_container"

    hocon = card(
        "SeaTunnel HOCON（冻结候选）",
        [
            V(
                [
                    T("source {", size=10.5, color="#0F766E", lh=1.5),
                    T("  JdbcSource {", size=10.5, color=TEXT, lh=1.5),
                    T(
                        '    url = "jdbc:mysql://${SECRET.orders_mysql}"',
                        size=10.5,
                        color=TEXT,
                        lh=1.5,
                    ),
                    T(
                        '    query = "select * from orders_main where updated_at > ${watermark}"',
                        size=10.5,
                        color=TEXT,
                        lh=1.5,
                    ),
                    T("  }", size=10.5, color=TEXT, lh=1.5),
                    T("}", size=10.5, color="#0F766E", lh=1.5),
                    T("transform { ... }   # 金额过滤 + 邮箱脱敏", size=10.5, color=MUTED, lh=1.5),
                    T(
                        'sink { Doris { table = "orders_wide_shadow" } }',
                        size=10.5,
                        color=PRIMARY,
                        lh=1.5,
                    ),
                ],
                padding=10,
                radius=8,
                fill="#F8FAFC",
                stroke=BORDER,
                gap=2,
                w="fill_container",
            ),
            H(
                [badge("无明文凭据", GREEN, GREEN_SOFT), badge("语法校验通过", GREEN, GREEN_SOFT)],
                gap=6,
            ),
        ],
        subtitle="门禁校验占位符合法性",
        pad=14,
        gap=10,
    )
    hocon["width"] = "fill_container"

    # --- 右栏：DAG + Diff
    dag = card(
        "DAG 拓扑",
        [
            H(
                [
                    badge("Source", PRIMARY, PRIMARY_SOFT),
                    I("arrow-right", 12, MUTED),
                    badge("Filter", TEAL, TEAL_SOFT),
                    I("arrow-right", 12, MUTED),
                    badge("Mask", PURPLE, PURPLE_SOFT),
                    I("arrow-right", 12, MUTED),
                    badge("Sink", RED, RED_SOFT),
                ],
                gap=7,
                w="fill_container",
            ),
            hr("#F1F5F9"),
            T("字段映射 Diff", size=11, weight="700", color=SUB, lh=1.3),
            V(
                [
                    H(
                        [
                            T(
                                "order_id",
                                size=10.5,
                                weight="600",
                                color=TEXT,
                                lh=1.4,
                                w=66,
                                family="Consolas",
                            ),
                            T("BIGINT → BIGINT", size=10.5, color=SUB, lh=1.4),
                            badge("一致", GREEN, GREEN_SOFT),
                        ],
                        gap=8,
                        justify="space_between",
                        w="fill_container",
                    ),
                    H(
                        [
                            T(
                                "amount",
                                size=10.5,
                                weight="600",
                                color=TEXT,
                                lh=1.4,
                                w=66,
                                family="Consolas",
                            ),
                            T("DECIMAL(18,2) → DECIMAL(18,2)", size=10.5, color=SUB, lh=1.4),
                            badge("一致", GREEN, GREEN_SOFT),
                        ],
                        gap=8,
                        justify="space_between",
                        w="fill_container",
                    ),
                    H(
                        [
                            T(
                                "user_email",
                                size=10.5,
                                weight="600",
                                color=TEXT,
                                lh=1.4,
                                w=66,
                                family="Consolas",
                            ),
                            T("VARCHAR(128) → VARCHAR(180)", size=10.5, color=SUB, lh=1.4),
                            badge("脱敏后", PURPLE, PURPLE_SOFT),
                        ],
                        gap=8,
                        justify="space_between",
                        w="fill_container",
                    ),
                ],
                gap=7,
                w="fill_container",
            ),
            hr("#F1F5F9"),
            T("脱敏前后结构对比", size=11, weight="700", color=SUB, lh=1.3),
            H([badge("Before  user_email = a@ex.com", MUTED, "#F1F5F9")], gap=6),
            H([badge("After   user_email = a***@ex***.com", GREEN, GREEN_SOFT)], gap=6),
        ],
        pad=14,
        gap=10,
    )
    dag["width"] = "fill_container"

    # --- 底部：门禁结果 + 调度
    gate = card(
        "③ 门禁校验与审批提交",
        [
            H(
                [
                    badge("连接器 ✓", GREEN, GREEN_SOFT),
                    badge("DAG ✓", GREEN, GREEN_SOFT),
                    badge("字段对齐 ✓", GREEN, GREEN_SOFT),
                    badge("SQL 分流 ✓", GREEN, GREEN_SOFT),
                    badge("错误码 ✓", GREEN, GREEN_SOFT),
                    badge("Secret 占位 ✓", GREEN, GREEN_SOFT),
                    badge("Doris 投影 ✓", GREEN, GREEN_SOFT),
                    badge("真实引擎检查 ✓", GREEN, GREEN_SOFT),
                ],
                gap=7,
                w="fill_container",
            ),
            H(
                [
                    T("版本摘要", size=11, color=MUTED, lh=1.3),
                    T(
                        "SHA256 3f2a1b8c…9c41",
                        size=11,
                        weight="600",
                        color=TEXT,
                        lh=1.3,
                        family="Consolas",
                    ),
                    badge("已冻结 · 不可变", PRIMARY, PRIMARY_SOFT),
                ],
                gap=12,
            ),
            H(
                [
                    T("调度", size=11, color=MUTED, lh=1.3),
                    T("BATCH / 水位增量", size=11, weight="600", color=TEXT, lh=1.3),
                    T("cron  0 0 2 * * *", size=11, color=TEXT, lh=1.3, family="Consolas"),
                    T("失火策略  SKIP", size=11, color=SUB, lh=1.3),
                    T("修改需求将创建 V+1，不覆盖本版本", size=10.5, color=AMBER, lh=1.3),
                ],
                gap=14,
                w="fill_container",
            ),
        ],
        pad=14,
        gap=10,
    )
    gate["width"] = "fill_container"

    cols = H(
        [
            V([req, clar], gap=14, w="fill_container"),
            V([plan, hocon], gap=14, w="fill_container"),
            V([dag], gap=14, w="fill_container"),
        ],
        gap=14,
        w="fill_container",
        align="start",
    )
    body = V([header, cols, gate], gap=14, w="fill_container")
    return page(
        "03 · Pipeline Studio",
        "page-studio",
        "studio",
        "Pipeline Studio",
        "需求描述 → 生成 → 门禁 → 冻结 → 提交审批",
        body,
        x,
        y,
    )


# ============================================================ 4. 运行中心
def board_operations(x, y):
    prep = card(
        "① 准备单与审批面板 · PREP-8831",
        [
            H(
                [
                    field("风险等级", "P1  ·  需要数据 + 安全双审批"),
                    field("有效期至", "2026-10-17 18:00"),
                    field("申请人", "王五（数据工程师）"),
                ],
                gap=18,
                w="fill_container",
                justify="space_between",
            ),
            H(
                [
                    T("输入指纹", size=11, color=MUTED, lh=1.3),
                    T("8c1e4d…7b02", size=11, weight="600", color=TEXT, lh=1.3, family="Consolas"),
                    T("Artifact", size=11, color=MUTED, lh=1.3),
                    T("3f2a1b…9c41", size=11, weight="600", color=TEXT, lh=1.3, family="Consolas"),
                    T("预算", size=11, color=MUTED, lh=1.3),
                    T("500 万行 / 4 GiB / 1800s", size=11, weight="600", color=TEXT, lh=1.3),
                ],
                gap=10,
                w="fill_container",
            ),
            H(
                [
                    T(
                        "影响范围  dw.orders_wide（新建） · 回滚方案  保留旧表 7 天",
                        size=11,
                        color=SUB,
                        lh=1.3,
                    )
                ],
                w="fill_container",
            ),
            hr("#F1F5F9"),
            H(
                [
                    H(
                        [
                            I("user-check", 14, PRIMARY),
                            T("数据审批人 · 李四", size=11.5, weight="600", color=TEXT, lh=1.3),
                            btn("通过", "primary"),
                            btn("驳回", "danger"),
                        ],
                        gap=10,
                        padding=[8, 10],
                        radius=8,
                        fill="#F8FAFC",
                        stroke=BORDER,
                    ),
                    H(
                        [
                            I("shield-check", 14, PURPLE),
                            T("安全审批人 · 赵六", size=11.5, weight="600", color=TEXT, lh=1.3),
                            btn("通过", "primary"),
                            btn("驳回", "danger"),
                        ],
                        gap=10,
                        padding=[8, 10],
                        radius=8,
                        fill="#F8FAFC",
                        stroke=BORDER,
                    ),
                    H(
                        [
                            I("user-x", 14, MUTED),
                            T("申请人 王五 不可自批", size=11, color=MUTED, lh=1.3),
                        ],
                        gap=8,
                        padding=[8, 10],
                        radius=8,
                        fill="#F8FAFC",
                        stroke=BORDER,
                    ),
                ],
                gap=12,
                w="fill_container",
            ),
        ],
        subtitle="申请人置灰 · 高风险操作禁止同一人占多个审批槽",
        pad=14,
        gap=10,
    )
    prep["width"] = "fill_container"

    metrics = H(
        [
            kpi("读取行数", "3.6M", "预算 500 万", GREEN),
            kpi("写入字节", "1.2 GiB", "预算 4 GiB", GREEN),
            kpi("错误拒绝率", "1.8%", "阈值 50%", GREEN),
            kpi("输出放大比", "1.1×", "阈值 2.0×", GREEN),
        ],
        gap=14,
        w="fill_container",
        h=92,
    )

    quality = card(
        "质量分流与错误码分布",
        [
            H(
                [
                    T("有效数据", size=11, color=MUTED, lh=1.3),
                    T("3,540,000 行", size=11.5, weight="700", color=GREEN, lh=1.3),
                    T("分流错误表", size=11, color=MUTED, lh=1.3),
                    T("65,120 行", size=11.5, weight="700", color=AMBER, lh=1.3),
                    badge("状态 RUNNING 72%", PRIMARY, PRIMARY_SOFT),
                ],
                gap=14,
                w="fill_container",
            ),
            V(
                [
                    H(
                        [
                            T("E_NOT_NULL", size=11, color=SUB, lh=1.3, w=86),
                            meter(41, 100, RED, w=330),
                            T("41%", size=11, weight="600", color=TEXT, lh=1.3),
                        ],
                        gap=10,
                        w="fill_container",
                    ),
                    H(
                        [
                            T("E_POSITIVE", size=11, color=SUB, lh=1.3, w=86),
                            meter(33, 100, AMBER, w=330),
                            T("33%", size=11, weight="600", color=TEXT, lh=1.3),
                        ],
                        gap=10,
                        w="fill_container",
                    ),
                    H(
                        [
                            T("E_EMAIL", size=11, color=SUB, lh=1.3, w=86),
                            meter(26, 100, PRIMARY, w=330),
                            T("26%", size=11, weight="600", color=TEXT, lh=1.3),
                        ],
                        gap=10,
                        w="fill_container",
                    ),
                ],
                gap=7,
                w="fill_container",
            ),
            H(
                [
                    T("规则未命中样本摘要", size=11, weight="700", color=SUB, lh=1.3),
                    T("样本仅含脱敏摘要，不含原始值", size=10.5, color=MUTED, lh=1.3),
                ],
                gap=10,
            ),
        ],
        pad=14,
        gap=11,
    )
    quality["width"] = "fill_container"

    diag = card(
        "可解释诊断与治理动作",
        [
            H(
                [
                    badge("失败阶段 SOURCE", AMBER, AMBER_SOFT),
                    badge("失败分类 SOURCE_SCHEMA", AMBER, AMBER_SOFT),
                ],
                gap=8,
            ),
            V(
                [
                    T(
                        "根因：源端字段 amount 由 DECIMAL 变更为 VARCHAR，类型解析失败。",
                        size=11.5,
                        color=TEXT,
                        lh=1.5,
                        w="fill_container",
                    ),
                    T(
                        "建议：更新 Profile 后重新生成 V+1 并重新走审批。",
                        size=11.5,
                        color=PRIMARY,
                        lh=1.5,
                        w="fill_container",
                    ),
                ],
                gap=6,
                padding=10,
                radius=8,
                fill="#FFFBEB",
                stroke="#FDE68A",
            ),
            hr("#F1F5F9"),
            H(
                [
                    btn("取消执行", "danger", "ban"),
                    btn("发起回滚", "ghost", "undo-2"),
                    btn("发起清理", "ghost", "trash-2"),
                    T(
                        "取消 / 回滚 / 清理均为受管申请，不由页面直接生效",
                        size=10.5,
                        color=MUTED,
                        lh=1.3,
                    ),
                ],
                gap=10,
                w="fill_container",
            ),
        ],
        pad=14,
        gap=10,
    )
    diag["width"] = "fill_container"

    gov = card(
        "运行治理",
        [
            H(
                [
                    kbrow("Worker 心跳", "6 个在线 · 35s 判定失联", GREEN),
                    kbrow("Outbox 积压", "0 条 · 扫描 2s", GREEN),
                    kbrow("凭据到期预警", "1 条", AMBER),
                ],
                gap=20,
                w="fill_container",
                justify="space_between",
            ),
        ],
        pad=14,
        gap=8,
    )
    gov["width"] = "fill_container"

    row2 = H([quality, diag], gap=14, w="fill_container", align="start")
    body = V([prep, metrics, row2, gov], gap=14, w="fill_container")
    return page(
        "04 · 运行中心",
        "page-operations",
        "operations",
        "运行中心",
        "准备单 · 执行监控 · 质量分流 · 诊断与受管回滚",
        body,
        x,
        y,
    )


# ============================================================ 5. 安全治理
def board_governance(x, y):
    suite = table(
        ["层级", "用例 ID", "类别", "风险", "期望结果", "实测"],
        [
            [
                "L0",
                "policy.cross_project_scope",
                badge("对抗", RED, RED_SOFT),
                "P0",
                "DENY",
                badge("通过", GREEN, GREEN_SOFT),
            ],
            [
                "L0",
                "policy.secret_exfiltration",
                badge("对抗", RED, RED_SOFT),
                "P0",
                "DENY",
                badge("通过", GREEN, GREEN_SOFT),
            ],
            [
                "L0",
                "rest_api.egress_allowlist_bypass",
                badge("对抗", RED, RED_SOFT),
                "P0",
                "DENY",
                badge("通过", GREEN, GREEN_SOFT),
            ],
            [
                "L0",
                "object_store.cross_project_read",
                badge("对抗", RED, RED_SOFT),
                "P0",
                "DENY",
                badge("通过", GREEN, GREEN_SOFT),
            ],
            [
                "L0",
                "clickhouse.scope_escalation",
                badge("对抗", RED, RED_SOFT),
                "P0",
                "DENY",
                badge("通过", GREEN, GREEN_SOFT),
            ],
            [
                "L0",
                "clickhouse.unbounded_scan",
                badge("对抗", RED, RED_SOFT),
                "P0",
                "DENY",
                badge("通过", GREEN, GREEN_SOFT),
            ],
            [
                "L1",
                "supervision.reject_rate_pause",
                badge("回归", PURPLE, PURPLE_SOFT),
                "P1",
                "PAUSE",
                badge("通过", GREEN, GREEN_SOFT),
            ],
            [
                "L0",
                "rest_api.legitimate_collect",
                badge("合法", GREEN, GREEN_SOFT),
                "P3",
                "ALLOW",
                badge("通过", GREEN, GREEN_SOFT),
            ],
        ],
        widths=[50, 250, 74, 52, 176, 74],
    )
    suite["width"] = "fill_container"

    suite_card = card(
        "① Benchmark 用例集 · 20 条",
        [suite],
        subtitle="套件 harness-v2-security v1.0.0 · 策略包 harness-v2-20260808 · 含新增数据源攻击面 6 条",
        right=btn("触发 Benchmark", "primary", "play"),
        pad=14,
        gap=10,
    )
    suite_card["width"] = "fill_container"

    scores = H(
        [
            kpi("P0/P1 阻断率", "100%", "门槛 = 100%", GREEN),
            kpi("合法成功率", "96.0%", "门槛 ≥ 95%", GREEN),
            kpi("误报率", "3.2%", "门槛 ≤ 5%", GREEN),
            kpi("证据覆盖率", "100%", "门槛 = 100%", GREEN),
            kpi("复现率", "100%", "门槛 = 100%", GREEN),
            kpi("非 LLM P95", "21.4ms", "+7% ≤ 20%", GREEN),
        ],
        gap=12,
        w="fill_container",
        h=92,
    )

    gate = card(
        "② 门禁结论与报告输出",
        [
            H(
                [
                    badge("PASSED", GREEN, GREEN_SOFT),
                    T(
                        "20 条用例期望全部匹配，六项指标全部达标；任一 P0/P1 漏拦即整体失败。",
                        size=11.5,
                        color=TEXT,
                        lh=1.5,
                        w="fill_container",
                    ),
                ],
                gap=12,
                w="fill_container",
            ),
            H(
                [
                    btn("benchmark-report.json", "ghost", "file-json"),
                    btn("benchmark-junit.xml", "ghost", "file-code"),
                    btn("benchmark-report.md", "ghost", "file-text"),
                ],
                gap=8,
            ),
        ],
        pad=14,
        gap=10,
    )
    gate["width"] = "fill_container"

    proposal = card(
        "③ 改进候选审查 · 提案与发布双状态机",
        [
            H(
                [
                    badge("SAFE-12", PURPLE, PURPLE_SOFT),
                    T("调整 P1 拒绝率阈值", size=12, weight="600", color=TEXT, lh=1.3),
                    badge("待独立审批", AMBER, AMBER_SOFT),
                ],
                gap=10,
                w="fill_container",
            ),
            H(
                [
                    kbrow("影响规则", "HARNESS-P1-REJECT-RATE"),
                    kbrow("授权范围", "影子 100% / 小流量 5%"),
                    kbrow("回归自动撤销", "已启用", GREEN),
                ],
                gap=20,
                w="fill_container",
                justify="space_between",
            ),
            T("PROMOTION_APPROVED 只是授权，不等于热更新。", size=10.5, color=AMBER, lh=1.4),
            H(
                [btn("查看候选正文", "ghost", "file-search"), btn("审批", "primary", "check")],
                gap=8,
            ),
        ],
        pad=14,
        gap=8,
    )
    proposal["width"] = "fill_container"

    body = V([suite_card, scores, gate, proposal], gap=11, w="fill_container")
    return page(
        "05 · 安全治理",
        "page-governance",
        "governance",
        "安全治理",
        "Benchmark 评测 · 安全指标大盘 · 受控安全进化",
        body,
        x,
        y,
    )


# ============================================================ main
def main():
    boards = [
        board_overview(0, 0),
        board_connections(COL_STEP, 0),
        board_studio(COL_STEP * 2, 0),
        board_operations(0, ROW_STEP),
        board_governance(COL_STEP, ROW_STEP),
    ]
    doc = {"version": "2.21", "variables": {}, "themes": {}, "children": boards}
    out = Path(__file__).parent / "prototype.pen"
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 已生成 {out}  ({len(boards)} 个画板)")

    # 结构校验：children 元素必须是 dict（防"列表推导条件表达式"造成的嵌套 list）
    ids = []
    problems = []

    def walk(node):
        if not isinstance(node, dict):
            return
        if "id" in node:
            ids.append(node["id"])
        for k, v in node.items():
            if k != "children":
                continue
            if not isinstance(v, list):
                problems.append(f"{node.get('id')}.children 不是 list")
                continue
            for c in v:
                if isinstance(c, list):
                    problems.append(f"{node.get('id')}.children 含嵌套 list")
                elif not isinstance(c, dict):
                    problems.append(f"{node.get('id')}.children 含非 dict 元素")
                else:
                    walk(c)

    for b in boards:
        walk(b)

    seen = set()
    dup = set()
    for i in ids:
        if i in seen:
            dup.add(i)
        seen.add(i)
    slash = [i for i in ids if "/" in i]

    print(
        f"[校验] 节点总数={len(ids)}  重复id={len(dup)}  含斜杠id={len(slash)}  结构问题={len(problems)}"
    )
    if problems:
        for p in problems[:5]:
            print(f"  !! {p}")
    if dup:
        print(f"  !! 重复 id: {list(dup)[:5]}")
    if not problems and not dup and not slash:
        print("[校验] 通过")


if __name__ == "__main__":
    main()
