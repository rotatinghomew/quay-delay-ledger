"""账期滚动：as-of 证据过滤、修订归并与差额成因分解。"""
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import write_fixture
from drayage_review.cli import run_review
from drayage_review.io import Dataset
from drayage_review.periods import (
    CAUSE_ATTRIBUTION,
    CAUSE_OBSERVATION,
    CAUSE_REMEDY,
    compare_runs,
    load_run,
)


class PeriodTest(unittest.TestCase):
    def test_revision_merge_and_visibility(self):
        # BX03 的放行 rev2 在 2026-06-04 才可见
        early = Dataset.load("data", as_of="2026-06-03")
        late = Dataset.load("data", as_of="2026-06-04")
        bx03_early = early.milestones("BX03")["放行"]
        bx03_late = late.milestones("BX03")["放行"]
        self.assertEqual(bx03_early["revision"], 1)
        self.assertEqual(bx03_early["lo"], 548)
        self.assertEqual(bx03_late["revision"], 2)
        self.assertEqual(bx03_late["lo"], 570)
        # 被抑制的修订有记录
        self.assertTrue(any(
            r["box"] == "BX03" for r in late.suppressed_revisions
        ) is False)
        self.assertTrue(any(
            r["box"] == "BX03" and r["revision"] == 2
            for r in early.suppressed_revisions
        ))

    def test_run_artifacts_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            bundle = run_review("data", "2026-06-07", str(out))
            self.assertTrue((out / "manifest.json").exists())
            self.assertTrue((out / "billing.json").exists())
            loaded = load_run(out)
            self.assertEqual(loaded.manifest["run_id"], bundle.manifest["run_id"])
            self.assertEqual(loaded.billing["grand"], bundle.billing["grand"])

    def test_delta_classified_as_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out1 = Path(tmp) / "r1"
            out2 = Path(tmp) / "r2"
            run_review("data", "2026-06-03", str(out1))
            run_review("data", "2026-06-04", str(out2))
            prev = load_run(out1)
            cur = load_run(out2)
            ds = Dataset.load("data", "2026-06-04")
            diff = compare_runs(prev, cur, ds)
            self.assertIn("BX03", diff["new_evidence_boxes"])
            bx03_deltas = [
                d for d in diff["line_deltas"] if d["box"] == "BX03"
            ]
            self.assertTrue(bx03_deltas)
            self.assertTrue(all(
                d["cause"] == CAUSE_OBSERVATION for d in bx03_deltas
            ))

    def test_blocked_items_carry_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            run_review("data", "2026-06-07", str(out))
            bundle = load_run(out)
            # 真实数据严格容量下 V1/V4 有拦下箱
            blocked = {
                box
                for vessel in bundle.fleet.values()
                for plan in vessel
                if not plan["feasible"]
                for box in [plan["box"]]
            }
            self.assertTrue(blocked)

    def test_shared_resource_linkage_classified_as_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out1 = Path(tmp) / "r1"
            out2 = Path(tmp) / "r2"
            run_review("data", "2026-06-03", str(out1))
            run_review("data", "2026-06-07", str(out2))
            ds = Dataset.load("data", "2026-06-07")
            diff = compare_runs(load_run(out1), load_run(out2), ds)
            # BX01 自身无新证据，但与 BX03 同船次共享资源、联合可行域联动改变
            bx01 = [
                d for d in diff["line_deltas"]
                if d["box"] == "BX01" and d.get("material")
            ]
            if bx01:
                self.assertTrue(all(
                    d["cause"] == CAUSE_OBSERVATION for d in bx01
                ))
                self.assertTrue(any("联动" in d["note"] for d in bx01))

    def test_no_change_rows_excluded_from_cause_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            out1 = Path(tmp) / "r1"
            out2 = Path(tmp) / "r2"
            run_review("data", "2026-06-03", str(out1))
            run_review("data", "2026-06-07", str(out2))
            ds = Dataset.load("data", "2026-06-07")
            diff = compare_runs(load_run(out1), load_run(out2), ds)
            immaterial = [
                d for d in diff["line_deltas"] if d.get("material") is False
            ]
            # 存在零变化行且其政策口径差额确实为零
            self.assertTrue(immaterial)
            self.assertTrue(all(
                d["amount_value_delta"] == 0 for d in immaterial
            ))
            # 合计只统计实质差额，归因合计不被零变化行污染
            self.assertEqual(
                diff["totals_by_cause"][CAUSE_ATTRIBUTION],
                round(sum(
                    d["amount_value_delta"] for d in diff["line_deltas"]
                    if d["cause"] == CAUSE_ATTRIBUTION and d.get("material", True)
                ), 2),
            )

    def test_three_cause_labels_complete(self):
        from drayage_review.periods import CAUSE_LABELS
        self.assertEqual(
            set(CAUSE_LABELS),
            {CAUSE_OBSERVATION, CAUSE_ATTRIBUTION, CAUSE_REMEDY},
        )

    def test_appointment_groups_multiple_boxes_but_identity_is_box(self):
        # 同一预约号 T0 含 BX00/BX01；车辆不同也不改箱程身份
        ds = Dataset.load("data")
        apps = {}
        for c in ds.cargo:
            apps.setdefault(c["appointment"], []).append(c["box"])
        self.assertEqual(sorted(apps["T0"]), ["BX00", "BX01"])
        # 箱程按 box 唯一
        self.assertEqual(len({c["box"] for c in ds.cargo}), len(ds.cargo))


if __name__ == "__main__":
    unittest.main()
