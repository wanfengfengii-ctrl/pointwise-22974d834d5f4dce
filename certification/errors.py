"""连续工段核证单的业务异常。

均继承 ValueError，与工程其余入口「业务不合规抛 ValueError」的约定一致；
细分类型便于调用方分别处理缺证、悬点关系与矛盾关系。
"""


class CertificationError(ValueError):
    """核证单相关业务错误的基类。"""


class MissingEvidenceError(CertificationError):
    """实收批缺少随批交接的跨页先后记录（旧件）。"""


class DanglingRelationError(CertificationError):
    """已登记关系指向本批不存在的原行。"""


class RelationConflictError(CertificationError):
    """已登记关系与页内原序或彼此之间形成矛盾（有环）。"""
