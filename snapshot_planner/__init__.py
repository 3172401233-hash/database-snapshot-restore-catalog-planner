"""快照恢复目录规划器。

仅根据清单与对象的元数据重建可恢复目录，不读取对象内容正文，
也不使用对象文件名或文件系统时间来判断归属关系。
"""

from .models import (
    DigestGroup,
    FileEvidence,
    Issue,
    Manifest,
    ObjectFile,
    RecoveryChain,
    RecoveryPlan,
    SnapshotCandidate,
)
from .planner import RecoveryPlanner
from .report import render_text_report

__all__ = [
    "DigestGroup",
    "FileEvidence",
    "Issue",
    "Manifest",
    "ObjectFile",
    "RecoveryChain",
    "RecoveryPlan",
    "RecoveryPlanner",
    "SnapshotCandidate",
    "render_text_report",
]

__version__ = "1.0.0"
