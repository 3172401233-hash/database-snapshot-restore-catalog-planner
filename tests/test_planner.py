"""恢复目录规划器的单元测试。"""

from __future__ import annotations

import unittest

from snapshot_planner import (
    Manifest,
    ObjectFile,
    RecoveryPlanner,
    render_text_report,
)
from snapshot_planner.models import (
    STATUS_BLOCKED,
    STATUS_INCOMPLETE,
    STATUS_RECOVERABLE,
)
from snapshot_planner.planner import (
    ISSUE_CROSS_TENANT,
    ISSUE_CYCLE,
    ISSUE_MISSING_FILE,
    ISSUE_MISSING_PARENT,
    ISSUE_ORPHAN_FILE,
)


def make_object(path, digest, tenant, database_id=None):
    return ObjectFile(
        path=path,
        digest=digest,
        tenant=tenant,
        database_id=database_id,
    )


def make_manifest(snapshot_id, parent, database_id, tenant, order, paths):
    return Manifest(
        snapshot_id=snapshot_id,
        parent_snapshot_id=parent,
        database_id=database_id,
        tenant=tenant,
        creation_order=order,
        referenced_paths=list(paths),
    )


class FullCatalogTest(unittest.TestCase):
    def test_chain_of_three_snapshots_is_recoverable_in_creation_order(self):
        objects = [
            make_object("db/a/base.parquet", "d1", "t1", "db1"),
            make_object("db/a/delta-2.parquet", "d2", "t1", "db1"),
            make_object("db/a/delta-3.parquet", "d3", "t1", "db1"),
        ]
        manifests = [
            make_manifest("s1", None, "db1", "t1", 1, ["db/a/base.parquet"]),
            make_manifest("s2", "s1", "db1", "t1", 2, ["db/a/base.parquet", "db/a/delta-2.parquet"]),
            make_manifest("s3", "s2", "db1", "t1", 3, ["db/a/delta-3.parquet"]),
        ]

        plan = RecoveryPlanner(objects, manifests).build_plan()

        self.assertEqual([item.snapshot_id for item in plan.candidates], ["s1", "s2", "s3"])
        self.assertTrue(all(item.status == STATUS_RECOVERABLE for item in plan.candidates))
        self.assertEqual([item.snapshot_id for item in plan.recoverable_snapshots()], ["s1", "s2", "s3"])

        s3 = plan.candidates[2]
        self.assertEqual(s3.chain.chain_snapshot_ids, ["s3", "s2", "s1"])
        self.assertTrue(s3.chain.complete)
        self.assertEqual([evidence.path for evidence in s3.files], ["db/a/delta-3.parquet"])
        self.assertEqual(s3.files[0].referenced_by_snapshots, ["s3"])
        self.assertEqual(plan.orphan_paths, [])
        self.assertEqual(plan.issues, [])

        report = render_text_report(plan)
        self.assertIn("快照恢复目录规划报告", report)
        self.assertIn("s3", report)


class DuplicateDigestTest(unittest.TestCase):
    def test_same_digest_across_databases_is_grouped_but_not_merged(self):
        objects = [
            make_object("store/x/part-1", "same-digest", "t1", "db1"),
            make_object("store/y/part-9", "same-digest", "t1", "db2"),
            make_object("store/z/part-2", "same-digest", "t2", "db3"),
        ]
        manifests = [
            make_manifest("s1", None, "db1", "t1", 1, ["store/x/part-1"]),
            make_manifest("s2", None, "db2", "t1", 2, ["store/y/part-9"]),
            make_manifest("s3", None, "db3", "t2", 3, ["store/z/part-2"]),
        ]

        plan = RecoveryPlanner(objects, manifests).build_plan()

        self.assertEqual(len(plan.digest_groups), 1)
        group = plan.digest_groups[0]
        self.assertEqual(group.digest, "same-digest")
        self.assertTrue(group.spans_databases)
        self.assertTrue(group.spans_tenants)
        self.assertEqual(group.database_ids, ["db1", "db2", "db3"])

        # 即使摘要完全相同，每个快照目录也只包含自己清单引用的对象。
        for candidate in plan.candidates:
            self.assertEqual(len(candidate.files), 1)
            self.assertEqual(candidate.status, STATUS_RECOVERABLE)
        self.assertEqual(
            plan.candidates[0].files[0].referenced_by_snapshots,
            ["s1"],
        )


class CyclicChainTest(unittest.TestCase):
    def test_direct_cycle_marks_every_node_on_chain_blocked(self):
        objects = [
            make_object("o1", "h1", "t1", "db1"),
            make_object("o2", "h2", "t1", "db1"),
            make_object("o3", "h3", "t1", "db1"),
            make_object("o4", "h4", "t1", "db1"),
        ]
        manifests = [
            make_manifest("s1", "s3", "db1", "t1", 1, ["o1"]),
            make_manifest("s2", "s1", "db1", "t1", 2, ["o2"]),
            make_manifest("s3", "s2", "db1", "t1", 3, ["o3"]),
            make_manifest("s4", "s3", "db1", "t1", 4, ["o4"]),
        ]

        plan = RecoveryPlanner(objects, manifests).build_plan()
        statuses = {item.snapshot_id: item.status for item in plan.candidates}
        self.assertEqual(
            statuses,
            {"s1": STATUS_BLOCKED, "s2": STATUS_BLOCKED, "s3": STATUS_BLOCKED, "s4": STATUS_BLOCKED},
        )

        cycle_issues = plan.issues_by_type(ISSUE_CYCLE)
        self.assertEqual(len(cycle_issues), 1)
        self.assertEqual(set(cycle_issues[0].related_snapshots), {"s1", "s2", "s3"})

        s4 = next(item for item in plan.candidates if item.snapshot_id == "s4")
        self.assertTrue(s4.chain.cyclic)
        self.assertEqual(set(s4.chain.cycle_snapshot_ids), {"s1", "s2", "s3"})

    def test_self_referencing_parent_is_a_cycle(self):
        objects = [make_object("o1", "h1", "t1", "db1")]
        manifests = [make_manifest("s1", "s1", "db1", "t1", 1, ["o1"])]

        plan = RecoveryPlanner(objects, manifests).build_plan()
        self.assertEqual(plan.candidates[0].status, STATUS_BLOCKED)
        self.assertEqual(plan.issues_by_type(ISSUE_CYCLE)[0].related_snapshots, ["s1"])


class MissingFileTest(unittest.TestCase):
    def test_missing_object_and_orphan_file_are_reported_separately(self):
        objects = [
            make_object("kept/1", "h1", "t1", "db1"),
            make_object("orphan/9", "h9", "t1", "db1"),
        ]
        manifests = [
            make_manifest("s1", None, "db1", "t1", 1, ["kept/1", "gone/missing"]),
        ]

        plan = RecoveryPlanner(objects, manifests).build_plan()

        candidate = plan.candidates[0]
        self.assertEqual(candidate.status, STATUS_INCOMPLETE)
        self.assertEqual(candidate.missing_paths, ["gone/missing"])
        missing_issue = plan.issues_by_type(ISSUE_MISSING_FILE)
        self.assertEqual([item.subject for item in missing_issue], ["gone/missing"])
        self.assertEqual(missing_issue[0].related_snapshots, ["s1"])

        self.assertEqual(plan.orphan_paths, ["orphan/9"])
        orphan_issue = plan.issues_by_type(ISSUE_ORPHAN_FILE)
        self.assertEqual([item.subject for item in orphan_issue], ["orphan/9"])

        evidence = next(item for item in candidate.files if item.path == "gone/missing")
        self.assertFalse(evidence.exists)
        self.assertIsNone(evidence.digest)

    def test_missing_parent_snapshot_breaks_chain_but_keeps_files(self):
        objects = [make_object("o1", "h1", "t1", "db1")]
        manifests = [make_manifest("s2", "ghost-parent", "db1", "t1", 2, ["o1"])]

        plan = RecoveryPlanner(objects, manifests).build_plan()
        candidate = plan.candidates[0]
        self.assertEqual(candidate.status, STATUS_INCOMPLETE)
        self.assertFalse(candidate.chain.complete)
        self.assertEqual(candidate.chain.missing_parent_ids, ["ghost-parent"])
        missing_parent_issue = plan.issues_by_type(ISSUE_MISSING_PARENT)
        self.assertEqual(missing_parent_issue[0].subject, "s2")


class CrossTenantTest(unittest.TestCase):
    def test_cross_tenant_reference_blocks_directory_membership(self):
        objects = [
            make_object("tenant-a/o1", "h1", "tenant-a", "db1"),
            make_object("tenant-b/o2", "h2", "tenant-b", "db2"),
        ]
        manifests = [
            make_manifest("s1", None, "db1", "tenant-a", 1, ["tenant-a/o1", "tenant-b/o2"]),
        ]

        plan = RecoveryPlanner(objects, manifests).build_plan()
        candidate = plan.candidates[0]
        self.assertEqual(candidate.status, STATUS_INCOMPLETE)
        self.assertEqual(candidate.cross_tenant_paths, ["tenant-b/o2"])

        cross_issues = plan.issues_by_type(ISSUE_CROSS_TENANT)
        self.assertEqual(len(cross_issues), 1)
        self.assertEqual(cross_issues[0].subject, "tenant-b/o2")

        evidence = next(item for item in candidate.files if item.path == "tenant-b/o2")
        self.assertTrue(evidence.tenant_mismatch)
        self.assertEqual(evidence.object_tenant, "tenant-b")
        self.assertEqual(evidence.manifest_tenant, "tenant-a")


class InputValidationTest(unittest.TestCase):
    def test_duplicate_object_path_and_snapshot_id_are_rejected(self):
        with self.assertRaises(ValueError):
            RecoveryPlanner(
                [
                    make_object("same/path", "h1", "t1", "db1"),
                    make_object("same/path", "h2", "t1", "db1"),
                ],
                [make_manifest("s1", None, "db1", "t1", 1, ["same/path"])],
            )

        with self.assertRaises(ValueError):
            RecoveryPlanner(
                [make_object("o1", "h1", "t1", "db1")],
                [
                    make_manifest("s1", None, "db1", "t1", 1, ["o1"]),
                    make_manifest("s1", None, "db1", "t1", 2, ["o1"]),
                ],
            )


class SerializationTest(unittest.TestCase):
    def test_plan_round_trips_through_dict(self):
        objects = [make_object("o1", "h1", "t1", "db1")]
        manifests = [make_manifest("s1", None, "db1", "t1", 1, ["o1"])]
        plan = RecoveryPlanner(objects, manifests).build_plan()

        data = plan.to_dict()
        self.assertEqual(data["candidates"][0]["snapshot_id"], "s1")
        self.assertEqual(data["candidates"][0]["chain"]["chain_snapshot_ids"], ["s1"])
        self.assertEqual(data["candidates"][0]["files"][0]["digest"], "h1")


if __name__ == "__main__":
    unittest.main()
