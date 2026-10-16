-- 主场景 A（PRD §9.12 / §10.2）源库：订单 + 用户，用于 MySQL → Doris 宽表增量同步。
-- updated_at 是水位增量列；amount 允许为负、email/customer_name 允许脏值，用于触发质量分流。
--
-- 必须显式声明连接字符集：入口脚本默认不以 utf8mb4 执行本文件，中文会被当成 latin1
-- 再编码一次，落库即乱码（实测踩过：`张三` 存成 `å¼ ä¸‰`）。
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS orders (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    user_id       BIGINT       NOT NULL,
    amount        DECIMAL(12, 2) NOT NULL,
    customer_name VARCHAR(128)     NULL,
    email         VARCHAR(191)     NULL,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY ix_orders_updated_at (updated_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS users (
    id         BIGINT      NOT NULL AUTO_INCREMENT,
    user_name  VARCHAR(128) NOT NULL,
    phone      VARCHAR(32)      NULL,
    region     VARCHAR(64)      NULL,
    created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

INSERT INTO users (id, user_name, phone, region) VALUES
    (1, '张三', '13800001234', '华东'),
    (2, '李四', '13900005678', '华北'),
    (3, '王五', '13700009012', '华南');

-- 有效行
INSERT INTO orders (user_id, amount, customer_name, email) VALUES
    (1, 199.00, '张三', 'zhangsan@example.com'),
    (2, 88.50,  '李四', 'lisi@example.com'),
    (3, 320.00, '王五', 'wangwu@example.com');

-- 脏行：负数金额（E_POSITIVE）、空客户名（E_NOT_NULL）、非法邮箱（E_EMAIL）
INSERT INTO orders (user_id, amount, customer_name, email) VALUES
    (1, -199.00, '张三', 'zhangsan@example.com'),
    (2, 0.00,    NULL,   'lisi@example.com'),
    (3, 45.00,   '王五', 'not-an-email');
