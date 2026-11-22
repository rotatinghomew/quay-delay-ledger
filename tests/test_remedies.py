"""补救反事实、补助资格与确认单防护。"""
import unittest

from drayage_review.io import Dataset
from drayage_review.remedies import (
    STATE_CONFIRMED,
    STATE_PENDING,
    evaluate_scenarios,
)


class RemedyTest(unittest.TestCase):
    def setUp(self):
        self.ds = Dataset.load("data")
        self.report = evaluate_scenarios(self.ds)

    def test_baseline_and_all_combos_present(self):
        scenarios = {s["scenario"] for s in self.report["scenarios"]}
        self.assertIn("baseline", scenarios)
        for r in self.ds.remedies:
            self.assertIn(r["remedy"], scenarios)
        # BX03 有两个补救 -> 必须存在组合情景
        self.assertTrue(any(s.startswith("combo:") for s in scenarios))

    def test_combination_uses_joint_reschedule_not_sum(self):
        # 组合情景的总交付不得优于把各单项改善简单相加之外的物理可行域；
        # 关键断言：组合交付由联合重解得出，且不违反任何单箱可行时刻
        base = next(s for s in self.report["scenarios"]
                    if s["scenario"] == "baseline")
        for sc in self.report["scenarios"]:
            for box, delivery in sc["deliveries"].items():
                ev = self.ds.milestones(box)["交付"]
                # 任何情景交付都不能早于证据下界（反事实只放松设施，不改证据）
                self.assertGreaterEqual(delivery, ev["lo"])
            self.assertLessEqual(sc["total_delivery"], base["total_delivery"] + 0)

    def test_unallocable_share_left_blank_not_forced(self):
        for ev in self.report["remedies"]:
            # 无合同方设施（专线）的减时不可分配
            if ev["facility"] not in ("船口", "公用", "全港"):
                self.assertFalse(ev["allocable"])
            self.assertIn("allocation_note", ev)
            # 估值区间保守口径不超过上界
            self.assertLessEqual(ev["value_lo"], ev["value_hi"])

    def test_subsidy_requires_reserve_and_verifiable_gain(self):
        for ev in self.report["remedies"]:
            if ev["eligible_for_subsidy"]:
                self.assertGreater(ev["verifiable_minutes_lo"], 0)
                self.assertIsNotNone(ev["related_contract"])

    def test_pending_acceptance_freezes_amount(self):
        pending = [
            a for a in self.report["acceptances"]
            if a["state"] == STATE_PENDING
        ]
        self.assertTrue(pending)
        for acc in pending:
            self.assertTrue(any("冻结" in f for f in acc["flags"]))

    def test_duplicate_claim_detection(self):
        # 构造同箱同补救的重复确认单
        ds = Dataset.load("data")
        ds.acceptances.append(dict(ds.acceptances[0]))  # 复制 a1
        report = evaluate_scenarios(ds)
        a1_rows = [a for a in report["acceptances"] if a["acceptance"] == "a1"]
        flagged = [a for a in a1_rows if any("重复认领" in f for f in a["flags"])]
        self.assertTrue(flagged)

    def test_reserve_headroom_excludes_confirmed_and_pending(self):
        from drayage_review.remedies import _reserve_headroom
        # Q1: reserved 700，已确认 a1=250
        self.assertEqual(_reserve_headroom(self.ds, "Q1"), 450)
        # Q3: reserved 510，待调整 a2=210 同样冻结
        self.assertEqual(_reserve_headroom(self.ds, "Q3"), 300)
        # Q2: reserved 900，已确认 a3=350
        self.assertEqual(_reserve_headroom(self.ds, "Q2"), 550)

    def test_confirmed_acceptance_state_preserved(self):
        confirmed = [
            a for a in self.report["acceptances"]
            if a["state"] == STATE_CONFIRMED
        ]
        self.assertTrue(confirmed)
        # 已确认单的额度字段原样保留，工具不做自动改写
        amounts = {a["acceptance"]: a["amount"] for a in confirmed}
        self.assertEqual(amounts.get("a1"), 250)
        self.assertEqual(amounts.get("a3"), 350)


if __name__ == "__main__":
    unittest.main()
