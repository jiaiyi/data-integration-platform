-- Checkpoint 与业务真相分库分账号：Agent 恢复数据不进入业务库治理范围。
CREATE USER dip_checkpoint WITH PASSWORD 'dip_checkpoint';
CREATE DATABASE dip_checkpoint OWNER dip_checkpoint;
