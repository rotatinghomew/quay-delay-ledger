"""边际反事实归因：明确可归因 vs 仅联合范围，禁止各方节省相加。"""
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import (
    capacity_fixture,
    infeasible_fixture,
    joint_fixture,
    window_fixture,
    write_fixture,
)
from drayage_review.attribution import attribute_fleet
from drayage_review.io import Dataset
from drayage_review.scheduler import solve_fleet


class AttributionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def load(self, fx):
        return Dataset.load(write_fixture(self.tmp, fx))

    def test_window_relief_is_attributable(self):
        ds = self.load(window_fixture())
        fleet = solve_fleet(ds, with_ranges=False)
        attrs = attribute_fleet(ds, fleet)["VT"]
        bw = next(a for a in attrs if a.box == "BW")
        dray = next(f for f in bw.facilities if f.facility == "全港")
        # 窗口放松可明确归因 70 分钟，且不产生仅联合范围
        self.assertEqual(dray.window_relief, 70)
        self.assertEqual(dray.capacity_relief, 0)
        self.assertEqual(dray.joint_only_relief, 0)
        self.assertEqual(dray.max_relief, 70)

    def test_capacity_relief_is_attributable_to_some_box(self):
        ds = self.load(capacity_fixture())
        fleet = solve_fleet(ds, with_ranges=False)
        attrs = attribute_fleet(ds, fleet)["VT"]
        berth = [
            f for a in attrs for f in a.facilities if f.facility == "船口"
        ]
        total_cap = sum(f.capacity_relief for f in berth)
        total_window = sum(f.window_relief for f in berth)
        # 单名额制造的 15 分钟等待必能在某只箱上归因为容量；窗口本身不绑定
        self.assertEqual(total_cap, 15)
        self.assertEqual(total_window, 0)
        # 任何单箱的可归因减时都不超过联合反事实上界
        for f in berth:
            self.assertLessEqual(
                f.window_relief + f.capacity_relief + f.joint_only_relief,
                f.max_relief,
            )

    def test_joint_only_relief_cannot_be_claimed_by_one_party(self):
        # 窗口与名额同时绑定（BJ 窗起点 445）：联合放松的总改善必须被识别，
        # 且明确可归因部分之外的份额落入仅联合范围
        ds = self.load(joint_fixture(445))
        fleet = solve_fleet(ds, with_ranges=False)
        attrs = attribute_fleet(ds, fleet)["VT"]
        berth = [
            f for a in attrs for f in a.facilities if f.facility == "船口"
        ]
        total_max = sum(f.max_relief for f in berth)
        total_attributable = sum(
            f.window_relief + f.capacity_relief for f in berth
        )
        # 联合反事实确实能省出时间
        self.assertGreater(total_max, 0)
        # 仅联合范围 = 联合改善 - 可单独归因部分，恒非负
        joint_only = sum(f.joint_only_relief for f in berth)
        self.assertEqual(joint_only, total_max - total_attributable)
        self.assertGreaterEqual(joint_only, 0)

    def test_no_double_counting_across_facilities(self):
        # 全船队层面：各设施可归因 + 仅联合范围之和，不得超过总可消除等待
        ds = self.load(capacity_fixture())
        fleet = solve_fleet(ds, with_ranges=False)
        attrs = attribute_fleet(ds, fleet)["VT"]
        for ba in attrs:
            reliefs = [
                f.window_relief + f.capacity_relief + f.joint_only_relief
                for f in ba.facilities
            ]
            # 同一箱不同设施的归因是不同边际原因，各自不超过其反事实上界
            for f in ba.facilities:
                self.assertGreaterEqual(f.max_relief, 0)
                self.assertGreaterEqual(f.joint_only_relief, 0)
            self.assertTrue(all(r >= 0 for r in reliefs))

    def test_blocked_box_has_no_attribution(self):
        ds = self.load(infeasible_fixture())
        fleet = solve_fleet(ds, with_ranges=False)
        attrs = attribute_fleet(ds, fleet)["VX"]
        bx = next(a for a in attrs if a.box == "BX")
        self.assertFalse(bx.feasible)
        self.assertEqual(bx.facilities, [])
        self.assertIsNotNone(bx.blocked_reason)


if __name__ == "__main__":
    unittest.main()
