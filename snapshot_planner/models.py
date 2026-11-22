"""规划器使用的元数据模型。

所有字段均来自清单或外部目录登记信息，对象内容正文永远不会被读取。
字段名同时提供 snake_case 的 Python 属性与 dict 构造别名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence


STATUS_RECOVERABLE = "recoverable"
STATUS_INCOMPLETE = "incomplete"
STATUS_BLOCKED = "blocked"

STATUS_LABELS = {
    STATUS_RECOVERABLE: "可恢复",
    STATUS_INCOMPLETE: "不完整",
    STATUS_BLOCKED: "已阻断",
}


def _pop(data, key, alias, default=None, required=False):
    if key in data:
        return data[key]
    if alias in data:
        return data[alias]
    if required:
        raise KeyError("缺少必填字段: %s" % key)
    return default


@dataclass
class ObjectFile:
    """存储中的对象文件（仅元数据）。

    ``path`` 是对象在存储中的唯一键；``digest`` 是外部登记的校验摘要；
    ``tenant`` 是对象归属租户。对象名与文件时间不参与快照归属判断。
    """

    path: str
    digest: str
    tenant: str
    database_id: Optional[str] = None
    size_bytes: Optional[int] = None

    @classmethod
    def from_dict(cls, data):
        return cls(
            path=_pop(data, "path", "object_path", required=True),
            digest=_pop(data, "digest", "checksum", required=True),
            tenant=_pop(data, "tenant", "tenant_id", required=True),
            database_id=_pop(data, "database_id", "databaseId"),
            size_bytes=_pop(data, "size_bytes", "size"),
        )

    def to_dict(self):
        return {
            "path": self.path,
            "digest": self.digest,
            "tenant": self.tenant,
            "database_id": self.database_id,
            "size_bytes": self.size_bytes,
        }


@dataclass
class Manifest:
    """零散快照清单：描述某一时间点引用了哪些对象。

    归属关系完全由 ``referenced_paths`` 建立，而不是对象名或时间。
    """

    snapshot_id: str
    parent_snapshot_id: Optional[str]
    database_id: str
    tenant: str
    creation_order: int
    referenced_paths: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data):
        return cls(
            snapshot_id=_pop(data, "snapshot_id", "snapshotId", required=True),
            parent_snapshot_id=_pop(data, "parent_snapshot_id", "parentSnapshotId"),
            database_id=_pop(data, "database_id", "databaseId", required=True),
            tenant=_pop(data, "tenant", "tenant_id", required=True),
            creation_order=_pop(data, "creation_order", "creationOrder", required=True),
            referenced_paths=list(_pop(data, "referenced_paths", "referencedPaths", default=[])),
        )

    def to_dict(self):
        return {
            "snapshot_id": self.snapshot_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "database_id": self.database_id,
            "tenant": self.tenant,
            "creation_order": self.creation_order,
            "referenced_paths": list(self.referenced_paths),
        }


@dataclass
class FileEvidence:
    """单个文件归属判断的证据。"""

    path: str
    digest: Optional[str]
    object_tenant: Optional[str]
    manifest_tenant: str
    database_id: str
    exists: bool
    referenced_by_snapshots: List[str] = field(default_factory=list)
    tenant_mismatch: bool = False

    def to_dict(self):
        return {
            "path": self.path,
            "digest": self.digest,
            "object_tenant": self.object_tenant,
            "manifest_tenant": self.manifest_tenant,
            "database_id": self.database_id,
            "exists": self.exists,
            "referenced_by_snapshots": list(self.referenced_by_snapshots),
            "tenant_mismatch": self.tenant_mismatch,
        }


@dataclass
class Issue:
    """规划过程中发现的需要单独报告的问题。"""

    issue_type: str
    severity: str
    subject: str
    detail: str
    related_snapshots: List[str] = field(default_factory=list)
    related_paths: List[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "issue_type": self.issue_type,
            "severity": self.severity,
            "subject": self.subject,
            "detail": self.detail,
            "related_snapshots": list(self.related_snapshots),
            "related_paths": list(self.related_paths),
        }


@dataclass
class RecoveryChain:
    """从根快照到目标快照的父链与可恢复性。"""

    target_snapshot_id: str
    chain_snapshot_ids: List[str] = field(default_factory=list)
    complete: bool = False
    cyclic: bool = False
    missing_parent_ids: List[str] = field(default_factory=list)
    cycle_snapshot_ids: List[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "target_snapshot_id": self.target_snapshot_id,
            "chain_snapshot_ids": list(self.chain_snapshot_ids),
            "complete": self.complete,
            "cyclic": self.cyclic,
            "missing_parent_ids": list(self.missing_parent_ids),
            "cycle_snapshot_ids": list(self.cycle_snapshot_ids),
        }


@dataclass
class SnapshotCandidate:
    """候选快照及其恢复目录、恢复链与证据。"""

    snapshot_id: str
    parent_snapshot_id: Optional[str]
    database_id: str
    tenant: str
    creation_order: int
    status: str
    files: List[FileEvidence] = field(default_factory=list)
    missing_paths: List[str] = field(default_factory=list)
    cross_tenant_paths: List[str] = field(default_factory=list)
    chain: Optional[RecoveryChain] = None

    def to_dict(self):
        return {
            "snapshot_id": self.snapshot_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "database_id": self.database_id,
            "tenant": self.tenant,
            "creation_order": self.creation_order,
            "status": self.status,
            "files": [item.to_dict() for item in self.files],
            "missing_paths": list(self.missing_paths),
            "cross_tenant_paths": list(self.cross_tenant_paths),
            "chain": self.chain.to_dict() if self.chain is not None else None,
        }


@dataclass
class DigestGroup:
    """相同校验摘要的对象分组。

    同摘要只说明字节内容可能一致，不代表属于同一数据库；
    跨数据库/跨租户时会显式标记，禁止据此合并快照目录。
    """

    digest: str
    paths: List[str] = field(default_factory=list)
    database_ids: List[str] = field(default_factory=list)
    tenants: List[str] = field(default_factory=list)
    spans_databases: bool = False
    spans_tenants: bool = False

    def to_dict(self):
        return {
            "digest": self.digest,
            "paths": list(self.paths),
            "database_ids": list(self.database_ids),
            "tenants": list(self.tenants),
            "spans_databases": self.spans_databases,
            "spans_tenants": self.spans_tenants,
        }


@dataclass
class RecoveryPlan:
    """规划结果：候选快照、恢复链、证据与问题清单。"""

    candidates: List[SnapshotCandidate] = field(default_factory=list)
    digest_groups: List[DigestGroup] = field(default_factory=list)
    orphan_paths: List[str] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)

    def recoverable_snapshots(self) -> List[SnapshotCandidate]:
        return [item for item in self.candidates if item.status == STATUS_RECOVERABLE]

    def issues_by_type(self, issue_type: str) -> List[Issue]:
        return [item for item in self.issues if item.issue_type == issue_type]

    def to_dict(self):
        return {
            "candidates": [item.to_dict() for item in self.candidates],
            "digest_groups": [item.to_dict() for item in self.digest_groups],
            "orphan_paths": list(self.orphan_paths),
            "issues": [item.to_dict() for item in self.issues],
        }


def sorted_unique(values: Sequence[str]) -> List[str]:
    return sorted(set(values))
