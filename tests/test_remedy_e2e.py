"""补救端到端：可核实减时贯通情景、估值、补助资格与保留额度。"""
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import window_fixture, write_fixture
from drayage_review.io import Dataset, write_json
from drayage_review.remedies import evaluate_scenarios


def _verifiable_gain_dataset(tmp: Path) -> Path:
    fx = window_fixture()
    # 把补救指向窗口夹具的箱 BW（短驳窗绑定，放松有可核实减时）
    fx["remedies"] = [
        {"remedy": "k1", "facility": "全港", "box": "BW", "gain": 30, "cost": 100},
    ]
    fx["acceptances"] = []
    return write_fixture(tmp, fx)


class RemedyEndToEndTest(unittest.TestCase):
    def test_verifiable_gain_flows_through_subsidy(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = _verifiable_gain_dataset(Path(tmp))
            ds = Dataset.load(data_dir)
            report = evaluate_scenarios(ds)
            k1 = next(e for e in report["remedies"] if e["remedy"] == "k1")
            # 反事实重解确实省出可核实交付时间
            self.assertGreater(k1["verifiable_minutes_lo"], 0)
            self.assertGreater(k1["verifiable_minutes_value"], 0)
            # 全港对应合同 T2，可分配且具备补助资格（保留额度 200 未占用）
            self.assertTrue(k1["allocable"])
            self.assertTrue(k1["eligible_for_subsidy"])
            self.assertEqual(k1["related_contract"], "T2")
            # 估值按 T2 费率 8 元/分钟
            self.assertEqual(
                k1["value_lo"], k1["verifiable_minutes_lo"] * 8
            )

    def test_scenario_delivery_matches_relaxation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = _verifiable_gain_dataset(Path(tmp))
            ds = Dataset.load(data_dir)
            report = evaluate_scenarios(ds)
            base = next(s for s in report["scenarios"]
                        if s["scenario"] == "baseline")
            single = next(s for s in report["scenarios"]
                          if s["scenario"] == "k1")
            self.assertLess(
                single["deliveries"]["BW"], base["deliveries"]["BW"]
            )
            # 改善不超过补救申请的提前幅度
            self.assertLessEqual(
                base["deliveries"]["BW"] - single["deliveries"]["BW"], 30
            )

    def test_reserve_blocks_when_headroom_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = _verifiable_gain_dataset(Path(tmp))
            ds = Dataset.load(data_dir)
            # 用确认单占满 T2 的保留额度 200
            ds.acceptances = [{
                "acceptance": "g9", "contract": "T2", "box": "BW",
                "remedy": "k1", "amount": 200, "state": "已确认",
                "day": "2026-07-02",
            }]
            report = evaluate_scenarios(ds)
            k1 = next(e for e in report["remedies"] if e["remedy"] == "k1")
            self.assertFalse(k1["eligible_for_subsidy"])
            self.assertIn("保留额度", k1["eligibility_note"])


if __name__ == "__main__":
    unittest.main()
