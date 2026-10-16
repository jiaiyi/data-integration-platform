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
