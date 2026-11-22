"""调度引擎：可行箱程、容量专占、窗口覆盖、冲突隔离与可行域包络。"""
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
from drayage_review.io import Dataset
from drayage_review.scheduler import RelaxSpec, solve_fleet, solve_vessel


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def load(self, fx):
        return Dataset.load(write_fixture(self.tmp, fx))

    def test_window_binding_relief(self):
        ds = self.load(window_fixture())
        fleet = solve_fleet(ds, with_ranges=False)
        plan = fleet["VT"]["plans"][0]
        self.assertTrue(plan.feasible)
        # 短驳窗 540 + 装运 30 -> 最早交付 570
        self.assertEqual(plan.services["dray"].start, 540)
        self.assertEqual(plan.delivery(), 570)
        # 放松短驳窗口后交付提前到证据下界 500（窗口放松减 70 分钟）
        relaxed = solve_vessel(
            ds, "VT", ["BW"],
            RelaxSpec(window_shift={("BW", "dray"): 720}),
            with_ranges=False,
        )
        plan2 = relaxed["plans"][0]
        self.assertEqual(plan2.delivery(), 500)
        self.assertEqual(plan.delivery() - plan2.delivery(), 70)

    def test_capacity_exclusivity_forces_queue(self):
        ds = self.load(capacity_fixture())
        fleet = solve_fleet(ds, with_ranges=False)
        starts = sorted(p.services["berth"].start for p in fleet["VT"]["plans"])
        # 单名额下两箱泊位开始时刻必须错开一个服务时长（15 分钟）
        self.assertEqual(starts, [430, 445])
        for p in fleet["VT"]["plans"]:
            for a in fleet["VT"]["plans"]:
                if a.box != p.box:
                    # 任意两箱同设施服务不得重叠
                    self.assertFalse(
                            p.services["berth"].start
                            < a.services["berth"].start + a.services["berth"].service
                            and a.services["berth"].start
                            < p.services["berth"].start + p.services["berth"].service
                    )

    def test_capacity_relief_removes_queue(self):
        ds = self.load(capacity_fixture())
        relaxed = solve_vessel(
            ds, "VT", ["BA", "BB"],
            RelaxSpec(relax_capacity_facilities={"船口"}),
            with_ranges=False,
        )
        starts = sorted(p.services["berth"].start for p in relaxed["plans"])
        # 名额放开后两箱都可在 430 开始
        self.assertEqual(starts, [430, 430])

    def test_infeasible_box_is_blocked_but_others_continue(self):
        # 合并一个可行箱与一个证据自相矛盾的箱
        ds = self.load(infeasible_fixture())
        fleet = solve_fleet(ds, with_ranges=False)
        plan = fleet["VX"]["plans"][0]
        self.assertFalse(plan.feasible)
        self.assertIsNotNone(plan.blocked_reason)
        self.assertIn("可行箱程", plan.blocked_reason)

    def test_partial_vessel_blocking(self):
        # 真实数据：V1/V4 班组能力不足时只拦下冲突箱，同船次其余箱继续复盘
        ds = Dataset.load("data")
        fleet = solve_fleet(ds, with_ranges=False)
        for vessel, result in fleet.items():
            boxes = result["plans"]
            if result["blocked"]:
                self.assertTrue(any(p.feasible for p in boxes)
                                or len(boxes) == len(result["blocked"]))
                for p in boxes:
                    if not p.feasible:
                        self.assertTrue(p.milestones == {})

    def test_service_windows_cover_their_milestones(self):
        ds = Dataset.load("data")
        fleet = solve_fleet(ds, with_ranges=False)
        for result in fleet.values():
            for plan in result["plans"]:
                if not plan.feasible:
                    continue
                cutoff = ds.cargo_by_box()[plan.box]["cutoff"]
                berth = plan.services["berth"]
                yard = plan.services["yard"]
                dray = plan.services["dray"]
                tv = plan.milestones["船侧交接"].value
                tr = plan.milestones["放行"].value
                th = plan.milestones["短驳接手"].value
                td = plan.milestones["交付"].value
                # 泊位覆盖船侧交接且在截单前
                self.assertLessEqual(berth.start, tv)
                self.assertLessEqual(tv, berth.end)
                self.assertLessEqual(berth.end, cutoff)
                # 堆场复核覆盖放行，整段落在窗口内
                self.assertLessEqual(yard.start, tr)
                self.assertLessEqual(tr, yard.end)
                self.assertGreaterEqual(yard.start, yard.open)
                self.assertLessEqual(yard.end, yard.close)
                # 短驳装运在两次交接之后、交付之前，整段落在窗口内
                self.assertGreaterEqual(dray.start, tr)
                self.assertGreaterEqual(dray.start, th)
                self.assertLessEqual(dray.end, td)
                self.assertGreaterEqual(dray.start, dray.open)
                self.assertLessEqual(dray.end, dray.close)

    def test_feasible_envelope_brackets_policy(self):
        ds = Dataset.load("data")
        fleet = solve_fleet(ds, with_ranges=True)
        for result in fleet.values():
            for plan in result["plans"]:
                if not plan.feasible:
                    continue
                for m in plan.milestones.values():
                    self.assertLessEqual(m.min, m.value + 0)
                    self.assertLessEqual(m.value, m.max)
                    self.assertLessEqual(m.lo, m.min)
                    self.assertGreaterEqual(m.hi, m.max)


if __name__ == "__main__":
    unittest.main()
