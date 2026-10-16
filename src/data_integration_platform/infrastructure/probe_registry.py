"""探查器注册表（T-11 / SPEC ADR-07）。

`ConnectorType` → `MetadataProbePort` 的唯一映射点。新增数据源只加一个 Adapter
与这里的一行，领域层与应用层零改动。

**未接入的连接器明确报错**，绝不静默降级或伪装成功（FR-07 验收口径）。
"""

from data_integration_platform.application.profiling import MetadataProbePort
from data_integration_platform.domain.enums import ConnectorType
from data_integration_platform.domain.errors import DomainError
from data_integration_platform.infrastructure.clickhouse_probe import ClickHouseMetadataProbe
from data_integration_platform.infrastructure.hdfs_probe import HdfsMetadataProbe
from data_integration_platform.infrastructure.hive_probe import HiveMetadataProbe
from data_integration_platform.infrastructure.local_file_probe import LocalFileMetadataProbe
from data_integration_platform.infrastructure.mysql_probe import MySqlMetadataProbe
from data_integration_platform.infrastructure.oracle_probe import OracleMetadataProbe
from data_integration_platform.infrastructure.postgresql_probe import PostgreSqlMetadataProbe
from data_integration_platform.infrastructure.rest_api_probe import RestApiMetadataProbe
from data_integration_platform.infrastructure.s3_probe import S3MetadataProbe
from data_integration_platform.infrastructure.sqlserver_probe import SqlServerMetadataProbe


class ProbeRegistry:
    """无状态注册表；探查器本身不持有连接，可被并发复用。"""

    def __init__(self) -> None:
        mysql = MySqlMetadataProbe(ConnectorType.MYSQL)
        self._probes: dict[ConnectorType, MetadataProbePort] = {
            ConnectorType.MYSQL: mysql,
            # Doris 以 MySQL 协议暴露元数据，复用同一实现（GAP-05 判定无缺口）。
            ConnectorType.DORIS: MySqlMetadataProbe(ConnectorType.DORIS),
            ConnectorType.POSTGRESQL: PostgreSqlMetadataProbe(),
            ConnectorType.ORACLE: OracleMetadataProbe(),
            ConnectorType.SQL_SERVER: SqlServerMetadataProbe(),
            ConnectorType.HIVE: HiveMetadataProbe(),
            ConnectorType.HDFS: HdfsMetadataProbe(),
            ConnectorType.CLICKHOUSE: ClickHouseMetadataProbe(),
            ConnectorType.S3_MINIO: S3MetadataProbe(),
            ConnectorType.LOCAL_FILE: LocalFileMetadataProbe(),
            ConnectorType.REST_API: RestApiMetadataProbe(),
        }

    def get(self, connector_type: ConnectorType) -> MetadataProbePort:
        probe = self._probes.get(connector_type)
        if probe is None:
            raise DomainError(f"{connector_type.value} 元数据探查器尚未接入")
        return probe

    def supported(self) -> frozenset[ConnectorType]:
        return frozenset(self._probes)


def build_registry() -> ProbeRegistry:
    return ProbeRegistry()
