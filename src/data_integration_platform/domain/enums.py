"""全量领域枚举（PRD §8.3）。

枚举一律以字符串存储（`native_enum=False`），避免 PostgreSQL 原生枚举在升级与
回滚时的迁移困难。Harness 专属词表见 `domain/harness.py`，监督动作见
`domain/supervision.py`，副作用与评测词表见各自模块。
"""

from enum import StrEnum


# --------------------------------------------------------------- 身份与项目治理
class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class ProjectStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ProjectRole(StrEnum):
    """项目成员角色：控制项目内资源的可见性与增删改查。"""

    ADMIN = "ADMIN"
    DATA_ENGINEER = "DATA_ENGINEER"
    APPROVER = "APPROVER"
    VIEWER = "VIEWER"


# --------------------------------------------------------- 管道版本与版本审批
class PipelineVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    PROFILING = "PROFILING"
    GENERATED = "GENERATED"
    VALIDATED = "VALIDATED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApprovalDecisionValue(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


# --------------------------------------------------------------- 连接与探查
class ConnectorType(StrEnum):
    """连接器类型。

    基线清单（PRD §8.4，必过）：MYSQL / POSTGRESQL / ORACLE / DORIS / CLICKHOUSE
    超集清单（决策 Q6，纳入验收）：SQL_SERVER / HIVE / HDFS
    数据源形态（基线明确要求）：LOCAL_FILE / S3_MINIO / REST_API
    """

    MYSQL = "MYSQL"
    POSTGRESQL = "POSTGRESQL"
    ORACLE = "ORACLE"
    DORIS = "DORIS"
    CLICKHOUSE = "CLICKHOUSE"
    SQL_SERVER = "SQL_SERVER"
    HIVE = "HIVE"
    HDFS = "HDFS"
    LOCAL_FILE = "LOCAL_FILE"
    S3_MINIO = "S3_MINIO"
    REST_API = "REST_API"


class ConnectionStatus(StrEnum):
    UNTESTED = "UNTESTED"
    TESTING = "TESTING"
    READY = "READY"
    FAILED = "FAILED"


class DataProfileStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    READY = "READY"
    FAILED = "FAILED"


class DataEndpointRole(StrEnum):
    SOURCE = "SOURCE"
    SINK = "SINK"


class FileAssetFormat(StrEnum):
    """受管文件格式。基线四项（CSV/XLSX/JSON/PARQUET）与超集五项均纳入验收（决策 Q6）。"""

    CSV = "CSV"
    XLSX = "XLSX"
    JSON = "JSON"
    JSONL = "JSONL"
    PARQUET = "PARQUET"
    ORC = "ORC"
    XML = "XML"
    MARKDOWN = "MARKDOWN"
    PDF = "PDF"


# --------------------------------------------------------------- 凭据治理
class SecretMigrationStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SecretRotationStatus(StrEnum):
    VALIDATING = "VALIDATING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ACTIVATING = "ACTIVATING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SecretRotationRollbackStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SecretDestructionStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SecretDestructionTarget(StrEnum):
    ROTATION_SOURCE = "ROTATION_SOURCE"
    ROLLED_BACK_STAGED = "ROLLED_BACK_STAGED"


class CredentialAlertLevel(StrEnum):
    WARNING = "WARNING"
    OVERDUE = "OVERDUE"


class CredentialAlertStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class CredentialAlertDeliveryStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# --------------------------------------------------------- 生成、执行与调度
class AgentRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    FAILED = "FAILED"


class GenerationKind(StrEnum):
    """模型一次调用的分类结果；状态机据此走澄清 / 门禁 / 修复三个分支。"""

    QUESTION = "question"
    CONFIG = "config"
    ERROR = "error"


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"


class ExecutionTrigger(StrEnum):
    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"


class ScheduleMode(StrEnum):
    BATCH = "BATCH"
    WATERMARK_INCREMENTAL = "WATERMARK_INCREMENTAL"


class ScheduleMisfirePolicy(StrEnum):
    SKIP = "SKIP"
    FIRE_ONCE = "FIRE_ONCE"


# --------------------------------------------------------------- 质量与诊断
class RollbackStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class CleanupStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class QualityStatus(StrEnum):
    PASSED = "PASSED"
    WARNING = "WARNING"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"


class FailureCategory(StrEnum):
    CONFIGURATION = "CONFIGURATION"
    CREDENTIALS = "CREDENTIALS"
    CONNECTIVITY = "CONNECTIVITY"
    SOURCE_SCHEMA = "SOURCE_SCHEMA"
    SINK_WRITE = "SINK_WRITE"
    ENGINE = "ENGINE"
    OBSERVABILITY = "OBSERVABILITY"
    CANCELLATION = "CANCELLATION"
    UNKNOWN = "UNKNOWN"


class FailureStage(StrEnum):
    SUBMISSION = "SUBMISSION"
    SOURCE = "SOURCE"
    TRANSFORM = "TRANSFORM"
    SINK = "SINK"
    MONITORING = "MONITORING"
    CANCELLATION = "CANCELLATION"
    PUBLISH = "PUBLISH"
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------- 审计
class AuditEventType(StrEnum):
    """审计只记录变化类型与摘要，不记录秘密（PRD FR-100）。"""

    PROJECT_CREATED = "PROJECT_CREATED"
    MEMBERSHIP_GRANTED = "MEMBERSHIP_GRANTED"
    MEMBERSHIP_REVOKED = "MEMBERSHIP_REVOKED"
    ROLE_GRANT_ASSIGNED = "ROLE_GRANT_ASSIGNED"
    ROLE_GRANT_REVOKED = "ROLE_GRANT_REVOKED"
    BOOTSTRAP_COMPLETED = "BOOTSTRAP_COMPLETED"
    CONNECTION_CREATED = "CONNECTION_CREATED"
    CONNECTION_UPDATED = "CONNECTION_UPDATED"
    CONNECTION_TESTED = "CONNECTION_TESTED"
    PROFILE_REQUESTED = "PROFILE_REQUESTED"
    PROFILE_COMPLETED = "PROFILE_COMPLETED"
    FILE_ASSET_UPLOADED = "FILE_ASSET_UPLOADED"
    FILE_ASSET_DELETED = "FILE_ASSET_DELETED"
    # 管道版本：冻结是不可回退的内容事实，单独成一类；修订另记一条，便于区分
    # 「新开了版本」与「往已有版本里写内容」（后者只可能发生在冻结之前）。
    PIPELINE_VERSION_CREATED = "PIPELINE_VERSION_CREATED"
    PIPELINE_VERSION_REVISED = "PIPELINE_VERSION_REVISED"
    PIPELINE_VERSION_FROZEN = "PIPELINE_VERSION_FROZEN"
    PIPELINE_VERSION_SUBMITTED = "PIPELINE_VERSION_SUBMITTED"
    PIPELINE_VERSION_DECIDED = "PIPELINE_VERSION_DECIDED"
    PIPELINE_SCHEDULE_CONFIGURED = "PIPELINE_SCHEDULE_CONFIGURED"
    PIPELINE_SCHEDULE_TOGGLED = "PIPELINE_SCHEDULE_TOGGLED"
    # 生成：受理一条、落地一条。中间过程留在 Checkpoint 库里，业务库只记结论。
    AGENT_RUN_REQUESTED = "AGENT_RUN_REQUESTED"
    AGENT_RUN_SETTLED = "AGENT_RUN_SETTLED"
    # 三阶段协议：每一步都要留下阶段事实，审批与提交才有可对齐的对象（FR-56）。
    EXECUTION_PREPARED = "EXECUTION_PREPARED"
    EXECUTION_APPROVED = "EXECUTION_APPROVED"
    EXECUTION_COMMITTED = "EXECUTION_COMMITTED"
    EXECUTION_SUBMITTED = "EXECUTION_SUBMITTED"
    # 凭据治理：四类受管副作用各自记录「申请 / 决策 / 执行」三个阶段，
    # 阶段粒度保留在事件类型里，便于按阶段筛查而不用解析摘要。
    SECRET_MIGRATION_REQUESTED = "SECRET_MIGRATION_REQUESTED"
    SECRET_MIGRATION_DECIDED = "SECRET_MIGRATION_DECIDED"
    SECRET_MIGRATION_EXECUTED = "SECRET_MIGRATION_EXECUTED"
    SECRET_ROTATION_REQUESTED = "SECRET_ROTATION_REQUESTED"
    SECRET_ROTATION_VALIDATED = "SECRET_ROTATION_VALIDATED"
    SECRET_ROTATION_DECIDED = "SECRET_ROTATION_DECIDED"
    SECRET_ROTATION_ACTIVATED = "SECRET_ROTATION_ACTIVATED"
    SECRET_ROTATION_ROLLBACK_REQUESTED = "SECRET_ROTATION_ROLLBACK_REQUESTED"
    SECRET_ROTATION_ROLLBACK_DECIDED = "SECRET_ROTATION_ROLLBACK_DECIDED"
    SECRET_ROTATION_ROLLBACK_EXECUTED = "SECRET_ROTATION_ROLLBACK_EXECUTED"
    SECRET_DESTRUCTION_REQUESTED = "SECRET_DESTRUCTION_REQUESTED"
    SECRET_DESTRUCTION_DECIDED = "SECRET_DESTRUCTION_DECIDED"
    SECRET_DESTRUCTION_EXECUTED = "SECRET_DESTRUCTION_EXECUTED"
    CREDENTIAL_ALERT_OPENED = "CREDENTIAL_ALERT_OPENED"
    CREDENTIAL_ALERT_RESOLVED = "CREDENTIAL_ALERT_RESOLVED"
    CREDENTIAL_ALERT_DELIVERED = "CREDENTIAL_ALERT_DELIVERED"
