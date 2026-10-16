-- 目标端初始化。主场景 A 的目标表 dw.orders_wide 属“新建表”（PRD §7.4 影响范围），
-- 由平台在发布阶段以影子表 + 原子 Swap 创建，这里不预置，避免掩盖“新建”语义。
CREATE DATABASE IF NOT EXISTS dw;
CREATE DATABASE IF NOT EXISTS ods;
-- 错误分流的落地库：按执行追加审计，不参与 Swap。
CREATE DATABASE IF NOT EXISTS ods_error;

-- 主场景 B（文件 → Doris 日志入库）的正式表。
CREATE TABLE IF NOT EXISTS ods.order_logs (
    log_id      BIGINT        NOT NULL,
    order_id    BIGINT        NULL,
    level       VARCHAR(16)   NULL,
    message     VARCHAR(1024) NULL,
    logged_at   DATETIME      NULL,
    ingested_at DATETIME      NULL
) ENGINE = OLAP
DUPLICATE KEY (log_id)
DISTRIBUTED BY HASH (log_id) BUCKETS 1
PROPERTIES ("replication_num" = "1");
