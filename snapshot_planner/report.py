"""规划结果的可读文本报告与简单命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Sequence

from .models import (
    Manifest,
    ObjectFile,
    RecoveryPlan,
    STATUS_LABELS,
)
from .planner import (
    ISSUE_CROSS_TENANT,
    ISSUE_CYCLE,
    ISSUE_MISSING_FILE,
    ISSUE_MISSING_PARENT,
    ISSUE_ORPHAN_FILE,
    RecoveryPlanner,
)

ISSUE_TITLES = {
    ISSUE_MISSING_FILE: "缺失文件",
    ISSUE_CROSS_TENANT: "跨租户引用",
    ISSUE_ORPHAN_FILE: "孤立文件",
    ISSUE_CYCLE: "循环父链",
    ISSUE_MISSING_PARENT: "缺失父快照",
}


def render_text_report(plan: RecoveryPlan) -> str:
    """把规划结果渲染为便于运维审阅的中文文本报告。"""
    lines: List[str] = []
    lines.append("快照恢复目录规划报告")
    lines.append("=" * 24)

    lines.append("")
    lines.append("一、候选快照（按创建顺序排序，不使用文件时间）")
    for candidate in plan.candidates:
        lines.append(
            "- %s [%s] 数据库=%s 租户=%s 创建顺序=%s"
            % (
                candidate.snapshot_id,
                STATUS_LABELS.get(candidate.status, candidate.status),
                candidate.database_id,
                candidate.tenant,
                candidate.creation_order,
            )
        )
        parent = candidate.parent_snapshot_id or "(无父快照)"
        chain = candidate.chain
        chain_text = " -> ".join(chain.chain_snapshot_ids) if chain else "(未计算)"
        lines.append("    父快照: %s" % parent)
        lines.append("    恢复链: %s" % chain_text)
        if chain and chain.missing_parent_ids:
            lines.append("    缺失父快照: %s" % ", ".join(chain.missing_parent_ids))
        if chain and chain.cycle_snapshot_ids:
            lines.append("    循环节点: %s" % ", ".join(chain.cycle_snapshot_ids))
        lines.append("    引用对象数: %d" % len(candidate.files))
        for evidence in candidate.files:
            mark = "OK" if evidence.exists and not evidence.tenant_mismatch else "异常"
            lines.append(
                "      [%s] %s 摘要=%s 引用方=%s"
                % (mark, evidence.path, evidence.digest or "(无)", ", ".join(evidence.referenced_by_snapshots))
            )
        if candidate.missing_paths:
            lines.append("    缺失路径: %s" % ", ".join(candidate.missing_paths))
        if candidate.cross_tenant_paths:
            lines.append("    跨租户路径: %s" % ", ".join(candidate.cross_tenant_paths))

    lines.append("")
    lines.append("二、可恢复快照")
    recoverable = plan.recoverable_snapshots()
    if recoverable:
        for candidate in recoverable:
            lines.append("- %s（恢复链: %s）" % (candidate.snapshot_id, " -> ".join(candidate.chain.chain_snapshot_ids)))
    else:
        lines.append("- 无")

    lines.append("")
    lines.append("三、重复校验摘要（同摘要不等于同数据库，禁止据此合并目录）")
    if plan.digest_groups:
        for group in plan.digest_groups:
            flags = []
            if group.spans_databases:
                flags.append("跨数据库")
            if group.spans_tenants:
                flags.append("跨租户")
            flag_text = "/".join(flags) if flags else "同一数据库内"
            lines.append(
                "- 摘要 %s [%s] 对象=%s 数据库=%s 租户=%s"
                % (
                    group.digest,
                    flag_text,
                    ", ".join(group.paths),
                    ", ".join(group.database_ids) or "(未登记)",
                    ", ".join(group.tenants),
                )
            )
    else:
        lines.append("- 无重复摘要")

    lines.append("")
    lines.append("四、孤立文件（未被任何清单引用，无法判断时间点）")
    lines.extend("- %s" % path for path in plan.orphan_paths) if plan.orphan_paths else lines.append("- 无")

    lines.append("")
    lines.append("五、问题清单")
    if plan.issues:
        for issue in plan.issues:
            lines.append(
                "- [%s/%s] %s: %s"
                % (
                    ISSUE_TITLES.get(issue.issue_type, issue.issue_type),
                    issue.severity,
                    issue.subject,
                    issue.detail,
                )
            )
    else:
        lines.append("- 无")

    return "\n".join(lines)


def load_input(payload: dict) -> RecoveryPlanner:
    """从 dict 结构构造规划器，输入只包含元数据。"""
    objects = [ObjectFile.from_dict(item) for item in payload.get("objects", [])]
    manifests = [Manifest.from_dict(item) for item in payload.get("manifests", [])]
    return RecoveryPlanner(objects, manifests)


def main(argv: Sequence[str] = None) -> int:
    parser = argparse.ArgumentParser(
        description="快照恢复目录规划器（仅使用元数据，不读取对象内容正文）",
    )
    parser.add_argument("input", help="JSON 输入文件，包含 objects 与 manifests 两个数组")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本报告")
    args = parser.parse_args(argv)

    with open(args.input, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    planner = load_input(payload)
    plan = planner.build_plan()
    if args.json:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text_report(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
