"""合同计费：窗口包络、免费时间、争议差额与拦截。"""
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import infeasible_fixture, write_fixture
from drayage_review.billing import build_billing, term_applies
from drayage_review.io import Dataset
from drayage_review.scheduler import solve_fleet


class BillingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_envelope_and_dispute_on_real_data(self):
        ds = Dataset.load("data")
        fleet = solve_fleet(ds, with_ranges=True)
        billing = build_billing(ds, fleet)
        for term in billing["terms"]:
            self.assertLessEqual(term["amount_lo"], term["amount_value"])
            self.assertLessEqual(term["amount_value"], term["amount_hi"])
            self.assertGreaterEqual(term["dispute_amount"], 0)
            for line in term["lines"]:
                if line["status"] == "拦下":
                    continue
                self.assertLessEqual(line["window_lo"], line["window_hi"])
                # 免费时间扣除后非负
                self.assertGreaterEqual(line["billable_lo"], 0)
                self.assertEqual(
                    line["dispute_amount"],
                    round(
                        (line["billable_hi"] - line["billable_lo"]) * line["rate"], 2
                    ),
                )

    def test_free_time_deducted(self):
        ds = Dataset.load("data")
        fleet = solve_fleet(ds, with_ranges=True)
        billing = build_billing(ds, fleet)
        q1 = next(t for t in billing["terms"] if t["contract"] == "Q1")
        term = ds.term("Q1")
        for line in q1["lines"]:
            if line["status"] == "拦下":
                continue
            self.assertEqual(
                line["billable_value"],
                max(0, line["window_value"] - term["free"]),
            )

    def test_blocked_box_lines_are_held_not_billed(self):
        ds = Dataset.load(write_fixture(self.tmp, infeasible_fixture()))
        fleet = solve_fleet(ds, with_ranges=False)
        billing = build_billing(ds, fleet)
        held = [
            line for t in billing["terms"] for line in t["lines"]
            if line["box"] == "BX"
        ]
        self.assertTrue(held)
        for line in held:
            self.assertEqual(line["status"], "拦下")
            self.assertEqual(line["amount_lo"], 0)
            self.assertEqual(line["amount_hi"], 0)
            self.assertIsNotNone(line["blocked_reason"])
        # 拦下箱不进入任何合同合计
        grand = billing["grand"]
        self.assertEqual(
            grand["amount_lo"],
            round(sum(
                line["amount_lo"] for t in billing["terms"]
                for line in t["lines"] if line["status"] != "拦下"
            ), 2),
        )

    def test_term_facility_matching(self):
        ds = Dataset.load("data")
        q3 = ds.term("Q3")  # 公用
        for cargo in ds.cargo:
            barriers = ds.box_barriers(cargo["box"])
            applies = term_applies(q3, barriers)
            has_public = any(b["facility"] == "公用" for b in barriers.values())
            self.assertEqual(applies, has_public)
        # 全港合同适用于全部箱
        q1 = ds.term("Q1")
        self.assertTrue(all(
            term_applies(q1, ds.box_barriers(c["box"])) for c in ds.cargo
        ))


if __name__ == "__main__":
    unittest.main()
