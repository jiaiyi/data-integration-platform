"""领域错误：API 层集中映射为 HTTP 状态码（SPEC §6.4）。"""


class DomainError(Exception):
    """可安全转换为 4xx 响应的领域错误。"""


class InvalidStateTransition(DomainError):
    """状态机拒绝跳步、非法回退与终态重启。"""


class PermissionDenied(DomainError):
    """权限或职责分离违规。"""


class VersionConflict(DomainError):
    """乐观锁冲突、摘要不匹配或幂等冲突。"""


class ResourceNotFound(DomainError):
    """资源不存在或对当前主体不可见。"""


class ExternalServiceUnavailable(DomainError):
    """外部依赖暂时不可用；API 映射 503，异步任务可按有限次数安全重试。"""


class SideEffectLeaseBusy(DomainError):
    """副作用执行租约被别人持有（SPEC §8.3）。

    与失败刻意分开：并发冲突是「现在不该做」，不是「做不成」。记成失败会让一次
    正常的重复投递毁掉一条本该成功的副作用，所以调用方应当**安全延迟重试**。
    """
