"""恢复目录规划核心逻辑。

判断文件是否属于同一张快照，只依据清单中的引用关系与租户/数据库元数据：

* 不读取对象内容正文，只使用登记好的校验摘要；
* 不使用对象文件名中的时间信息或文件系统时间排序；
* 摘要相同仅表示内容可能一致，不代表属于同一数据库。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .models import (
    DigestGroup,
    FileEvidence,
    Issue,
    Manifest,
    ObjectFile,
    RecoveryChain,
    RecoveryPlan,
    SnapshotCandidate,
    STATUS_BLOCKED,
    STATUS_INCOMPLETE,
    STATUS_RECOVERABLE,
    sorted_unique,
)

ISSUE_MISSING_FILE = "missing_file"
ISSUE_CROSS_TENANT = "cross_tenant_reference"
ISSUE_ORPHAN_FILE = "orphan_file"
ISSUE_CYCLE = "cyclic_parent_chain"
ISSUE_MISSING_PARENT = "missing_parent_snapshot"


class RecoveryPlanner:
    """根据对象文件与零散清单重建可恢复快照目录。"""

    def __init__(
        self,
        objects: Sequence[ObjectFile],
        manifests: Sequence[Manifest],
    ) -> None:
        self._objects: Dict[str, ObjectFile] = {}
        for obj in objects:
            if obj.path in self._objects:
                raise ValueError("对象路径重复: %s" % obj.path)
            self._objects[obj.path] = obj

        self._manifests: Dict[str, Manifest] = {}
        for manifest in manifests:
            if manifest.snapshot_id in self._manifests:
                raise ValueError("快照 ID 重复: %s" % manifest.snapshot_id)
            self._manifests[manifest.snapshot_id] = manifest

        self._referenced_paths: Set[str] = set()
        for manifest in manifests:
            self._referenced_paths.update(manifest.referenced_paths)

    def build_plan(self) -> RecoveryPlan:
        """执行完整规划并返回候选快照、恢复链与证据。"""
        issues: List[Issue] = []
        issues.extend(self._check_missing_files())
        issues.extend(self._check_cross_tenant_references())
        issues.extend(self._check_orphan_files())

        cycle_info = self._analyze_parent_chains(issues)
        candidates = self._build_candidates(cycle_info, issues)
        digest_groups = self._build_digest_groups()

        return RecoveryPlan(
            candidates=candidates,
            digest_groups=digest_groups,
            orphan_paths=self._find_orphan_paths(),
            issues=issues,
        )

    def _check_missing_files(self) -> List[Issue]:
        issues = []
        for path in sorted(self._referenced_paths):
            if path not in self._objects:
                owners = self._manifests_referencing(path)
                issues.append(
                    Issue(
                        issue_type=ISSUE_MISSING_FILE,
                        severity="error",
                        subject=path,
                        detail="清单引用的对象在存储目录中不存在，无法纳入恢复目录",
                        related_snapshots=owners,
                        related_paths=[path],
                    )
                )
        return issues

    def _check_cross_tenant_references(self) -> List[Issue]:
        issues = []
        seen = set()
        for manifest in sorted(self._manifests.values(), key=lambda item: (item.creation_order, item.snapshot_id)):
            for path in manifest.referenced_paths:
                obj = self._objects.get(path)
                if obj is None or obj.tenant == manifest.tenant:
                    continue
                key = (path, manifest.snapshot_id)
                if key in seen:
                    continue
                seen.add(key)
                issues.append(
                    Issue(
                        issue_type=ISSUE_CROSS_TENANT,
                        severity="error",
                        subject=path,
                        detail=(
                            "清单 %s 的租户为 %s，但对象归属租户为 %s，禁止跨租户纳入恢复目录"
                            % (manifest.snapshot_id, manifest.tenant, obj.tenant)
                        ),
                        related_snapshots=[manifest.snapshot_id],
                        related_paths=[path],
                    )
                )
        return issues

    def _find_orphan_paths(self) -> List[str]:
        return sorted(path for path in self._objects if path not in self._referenced_paths)

    def _check_orphan_files(self) -> List[Issue]:
        issues = []
        for path in self._find_orphan_paths():
            obj = self._objects[path]
            issues.append(
                Issue(
                    issue_type=ISSUE_ORPHAN_FILE,
                    severity="warning",
                    subject=path,
                    detail=(
                        "对象未被任何清单引用，无法判断其快照时间点，"
                        "登记租户=%s，摘要=%s" % (obj.tenant, obj.digest)
                    ),
                    related_snapshots=[],
                    related_paths=[path],
                )
            )
        return issues

    def _manifests_referencing(self, path: str) -> List[str]:
        owners = [
            manifest.snapshot_id
            for manifest in self._manifests.values()
            if path in manifest.referenced_paths
        ]
        return sorted(owners)

    def _analyze_parent_chains(self, issues: List[Issue]) -> Dict[str, RecoveryChain]:
        """为每张快照计算父链，识别缺失父快照与循环父链。"""
        chains: Dict[str, RecoveryChain] = {}
        cyclic_nodes: Set[str] = set()
        cycle_members_reported: Set[Tuple[str, ...]] = set()

        for start_id in self._manifests:
            chain_snapshot_ids: List[str] = []
            seen: Dict[str, int] = {}
            current = start_id
            missing_parent_ids: List[str] = []
            cycle_snapshot_ids: List[str] = []
            cyclic = False

            while current is not None:
                if current in seen:
                    cyclic = True
                    cycle_start = seen[current]
                    cycle_snapshot_ids = chain_snapshot_ids[cycle_start:]
                    cyclic_nodes.update(chain_snapshot_ids[cycle_start:])
                    break
                if current not in self._manifests:
                    missing_parent_ids.append(current)
                    break
                seen[current] = len(chain_snapshot_ids)
                chain_snapshot_ids.append(current)
                current = self._manifests[current].parent_snapshot_id

            chain = RecoveryChain(
                target_snapshot_id=start_id,
                chain_snapshot_ids=list(chain_snapshot_ids),
                complete=not cyclic and not missing_parent_ids,
                cyclic=cyclic,
                missing_parent_ids=sorted_unique(missing_parent_ids),
                cycle_snapshot_ids=list(cycle_snapshot_ids),
            )
            chains[start_id] = chain

            if cyclic:
                marker = self._canonical_cycle(cycle_snapshot_ids)
                if marker not in cycle_members_reported:
                    cycle_members_reported.add(marker)
                    issues.append(
                        Issue(
                            issue_type=ISSUE_CYCLE,
                            severity="error",
                            subject=" -> ".join(cycle_snapshot_ids + [cycle_snapshot_ids[0]]),
                            detail="父快照链存在循环，链上快照无法定位到一致的恢复时间点",
                            related_snapshots=list(cycle_snapshot_ids),
                        )
                    )

        for manifest in self._manifests.values():
            parent_id = manifest.parent_snapshot_id
            if parent_id is not None and parent_id not in self._manifests:
                issues.append(
                    Issue(
                        issue_type=ISSUE_MISSING_PARENT,
                        severity="error",
                        subject=manifest.snapshot_id,
                        detail="快照 %s 声明的父快照 %s 缺失，恢复链在父链处断裂"
                        % (manifest.snapshot_id, parent_id),
                        related_snapshots=[manifest.snapshot_id, parent_id],
                    )
                )

        # 能沿父链走到循环节点的快照同样不可恢复，即使自身不在环上。
        for snapshot_id, chain in chains.items():
            if chain.cyclic:
                continue
            if any(node in cyclic_nodes for node in chain.chain_snapshot_ids):
                chain.cyclic = True
                chain.complete = False
                chain.cycle_snapshot_ids = sorted(
                    node for node in chain.chain_snapshot_ids if node in cyclic_nodes
                )

        return chains

    @staticmethod
    def _canonical_cycle(cycle_snapshot_ids: List[str]) -> Tuple[str, ...]:
        """把环旋转到最小快照 ID 开头，使同一环只有一个去重标记。"""
        if not cycle_snapshot_ids:
            return tuple()
        ordered = list(cycle_snapshot_ids)
        offset = ordered.index(min(ordered))
        return tuple(ordered[offset:] + ordered[:offset])

    def _build_candidates(
        self,
        chains: Dict[str, RecoveryChain],
        issues: List[Issue],
    ) -> List[SnapshotCandidate]:
        candidates: List[SnapshotCandidate] = []
        ordered = sorted(
            self._manifests.values(),
            key=lambda item: (item.creation_order, item.snapshot_id),
        )
        for manifest in ordered:
            files: List[FileEvidence] = []
            missing_paths: List[str] = []
            cross_tenant_paths: List[str] = []

            for path in sorted(set(manifest.referenced_paths)):
                obj = self._objects.get(path)
                tenant_mismatch = obj is not None and obj.tenant != manifest.tenant
                if obj is None:
                    missing_paths.append(path)
                if tenant_mismatch:
                    cross_tenant_paths.append(path)
                files.append(
                    FileEvidence(
                        path=path,
                        digest=obj.digest if obj is not None else None,
                        object_tenant=obj.tenant if obj is not None else None,
                        manifest_tenant=manifest.tenant,
                        database_id=manifest.database_id,
                        exists=obj is not None,
                        referenced_by_snapshots=self._manifests_referencing(path),
                        tenant_mismatch=tenant_mismatch,
                    )
                )

            chain = chains[manifest.snapshot_id]
            if chain.cyclic:
                status = STATUS_BLOCKED
            elif missing_paths or cross_tenant_paths or not chain.complete:
                status = STATUS_INCOMPLETE
            else:
                status = STATUS_RECOVERABLE

            candidates.append(
                SnapshotCandidate(
                    snapshot_id=manifest.snapshot_id,
                    parent_snapshot_id=manifest.parent_snapshot_id,
                    database_id=manifest.database_id,
                    tenant=manifest.tenant,
                    creation_order=manifest.creation_order,
                    status=status,
                    files=files,
                    missing_paths=missing_paths,
                    cross_tenant_paths=cross_tenant_paths,
                    chain=chain,
                )
            )
        return candidates

    def _build_digest_groups(self) -> List[DigestGroup]:
        grouped: Dict[str, List[ObjectFile]] = defaultdict(list)
        for obj in self._objects.values():
            grouped[obj.digest].append(obj)

        result: List[DigestGroup] = []
        for digest in sorted(grouped):
            members = sorted(grouped[digest], key=lambda item: item.path)
            if len(members) <= 1:
                continue
            database_ids = sorted_unique(
                item.database_id for item in members if item.database_id is not None
            )
            tenants = sorted_unique(item.tenant for item in members)
            result.append(
                DigestGroup(
                    digest=digest,
                    paths=[item.path for item in members],
                    database_ids=database_ids,
                    tenants=tenants,
                    spans_databases=len(database_ids) > 1,
                    spans_tenants=len(tenants) > 1,
                )
            )
        return result
