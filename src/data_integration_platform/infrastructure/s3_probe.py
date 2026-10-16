"""S3 / MinIO 对象存储探查器（T-16 / GAP-02）。

需求基线 §2.8.1 明确要求对象存储接入，参考实现只有本地文件，属必须补齐的真实缺口。
MinIO 客户端说 S3 协议，同一实现同时覆盖 MinIO 与 AWS S3。

**只取对象元数据 + 前缀范围采样，严禁全量下载**（决策 D-Q3）：读取量由
`plan_remote_read` 统一决定，对象超过上限时文本类格式只读前缀、结构在尾部的格式直接报错。
"""

from collections.abc import Sequence
from typing import Any

import urllib3
from minio import Minio
from minio.error import MinioException, S3Error

from data_integration_platform.application.profiling import ProbeResult, ProbeTarget
from data_integration_platform.application.secrets import ConnectionCredentials
from data_integration_platform.config import get_settings
from data_integration_platform.domain.enums import ConnectorType
from data_integration_platform.domain.errors import (
    DomainError,
    ExternalServiceUnavailable,
    ResourceNotFound,
)
from data_integration_platform.infrastructure.probe_support import (
    describe_failure,
    require_relative_path,
    require_username,
    run_blocking,
)
from data_integration_platform.infrastructure.remote_probe import (
    build_remote_probe_result,
    plan_remote_read,
)

DEFAULT_PORT = 9000


class S3MetadataProbe:
    connector_type = ConnectorType.S3_MINIO

    async def test_connection(
        self, *, target: ProbeTarget, credentials: ConnectionCredentials
    ) -> None:
        bucket = _resolve_bucket(target, schema_name=None)
        exists = await run_blocking(
            self._bucket_exists, target=target, credentials=credentials, bucket=bucket
        )
        if not exists:
            raise ResourceNotFound(f"对象存储桶不存在：{bucket}")

    async def list_tables(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str | None,
        limit: int,
    ) -> Sequence[str]:
        bucket = _resolve_bucket(target, schema_name)
        prefix = target.options.get("prefix", "")
        return await run_blocking(
            self._list_blocking,
            target=target,
            credentials=credentials,
            bucket=bucket,
            prefix=prefix,
            limit=limit,
        )

    async def probe_table(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        schema_name: str,
        table_name: str,
        sample_limit: int,
    ) -> ProbeResult:
        bucket = _resolve_bucket(target, schema_name)
        key = require_relative_path(table_name, kind="对象键")
        metadata = await run_blocking(
            self._stat_blocking, target=target, credentials=credentials, bucket=bucket, key=key
        )
        size = int(metadata["size"])
        plan = plan_remote_read(
            object_name=key,
            size_bytes=size,
            cap_bytes=get_settings().remote_file_sample_bytes,
        )
        data = await run_blocking(
            self._read_blocking,
            target=target,
            credentials=credentials,
            bucket=bucket,
            key=key,
            length=plan.read_length,
        )
        return build_remote_probe_result(
            plan=plan,
            data=data,
            schema_name=bucket,
            object_name=key,
            size_bytes=size,
            sample_limit=sample_limit,
            statistics={
                "etag": str(metadata.get("etag") or ""),
                "last_modified": str(metadata.get("last_modified") or ""),
                "content_type": str(metadata.get("content_type") or ""),
            },
        )

    # ---------------------------------------------------------- 同步实现
    def _client(self, *, target: ProbeTarget, credentials: ConnectionCredentials) -> Minio:
        if not target.host:
            raise DomainError("对象存储连接缺少端点地址")
        access_key = credentials.access_key or require_username(
            credentials, connector=self.connector_type.value
        )
        if credentials.secret_key is None:
            raise DomainError("对象存储连接缺少 Secret Key")
        timeout = get_settings().probe_statement_timeout_seconds
        return Minio(
            endpoint=f"{target.host}:{target.port or DEFAULT_PORT}",
            access_key=access_key,
            secret_key=credentials.secret_key.get_secret_value(),
            secure=target.options.get("secure", "false").casefold() == "true",
            region=target.options.get("region") or None,
            # Minio 构造器不接受 timeout，只能从 http_client 注入：
            # 超时按探查上限收敛，且不做无限重试，避免一次探查挂住控制面线程。
            http_client=urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=timeout, read=timeout),
                retries=urllib3.Retry(total=2, backoff_factor=0.2),
            ),
        )

    def _bucket_exists(
        self, *, target: ProbeTarget, credentials: ConnectionCredentials, bucket: str
    ) -> bool:
        try:
            return bool(self._client(target=target, credentials=credentials).bucket_exists(bucket))
        except (MinioException, S3Error) as exc:
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 连接")
            ) from exc

    def _list_blocking(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        bucket: str,
        prefix: str,
        limit: int,
    ) -> list[str]:
        client = self._client(target=target, credentials=credentials)
        try:
            names: list[str] = []
            for item in client.list_objects(bucket, prefix=prefix or None, recursive=True):
                names.append(str(item.object_name))
                # 枚举同样有界：不在前缀范围内无限翻页。
                if len(names) >= limit:
                    break
            return names
        except (MinioException, S3Error) as exc:
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 枚举对象")
            ) from exc

    def _stat_blocking(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        bucket: str,
        key: str,
    ) -> dict[str, Any]:
        """只取对象元数据（size / etag / last_modified），不读对象内容。"""
        client = self._client(target=target, credentials=credentials)
        try:
            stat = client.stat_object(bucket, key)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                raise ResourceNotFound(f"对象不存在：{key}") from exc
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 读取元数据")
            ) from exc
        except (MinioException, OSError) as exc:
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 读取元数据")
            ) from exc
        return {
            "size": stat.size,
            "etag": stat.etag,
            "last_modified": stat.last_modified,
            "content_type": stat.content_type,
        }

    def _read_blocking(
        self,
        *,
        target: ProbeTarget,
        credentials: ConnectionCredentials,
        bucket: str,
        key: str,
        length: int,
    ) -> bytes:
        """前缀范围读取：`offset=0, length=N`，绝不整份下载。"""
        client = self._client(target=target, credentials=credentials)
        try:
            response = client.get_object(bucket, key, offset=0, length=length)
            try:
                return bytes(response.read())
            finally:
                response.close()
                response.release_conn()
        except (MinioException, S3Error, OSError) as exc:
            raise ExternalServiceUnavailable(
                describe_failure(exc, context=f"{self.connector_type.value} 读取对象前缀")
            ) from exc


def _resolve_bucket(target: ProbeTarget, schema_name: str | None) -> str:
    bucket = (schema_name or target.options.get("bucket") or "").strip()
    if not bucket:
        raise DomainError("对象存储探查需要指定桶名（Bucket）")
    return require_relative_path(bucket, kind="桶名")
