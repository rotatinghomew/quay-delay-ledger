"""受控合成箱程夹具，用于验证调度引擎的约束生效路径。

真实 ``data/`` 的设施窗口较松，窗口/名额约束大多不卡在关键路径上；这里提供
四个聚焦场景，每个只激活一条因果通道，便于严格断言：

* ``window_fixture``   单箱，设施窗口起点绑定交付，放松窗口即减时；
* ``capacity_fixture`` 两箱证据相同、泊位窗强制重叠，单名额产生可归因等待；
* ``joint_fixture``    窗口与名额同时绑定，只有联合放松才有减时（仅联合范围）；
* ``infeasible_fixture`` 证据自相矛盾的箱，无可行箱程须被隔离。
"""
from __future__ import annotations

import copy
from pathlib import Path

from drayage_review.io import write_json

BASE_DAY = "2026-07-01"


def _event(eid, box, kind, lo, hi, available=BASE_DAY, revision=1, vehicle=None):
    return {
        "event": eid, "box": box, "kind": kind, "day": BASE_DAY,
        "lo": lo, "hi": hi, "available": available, "revision": revision,
        "vehicle": vehicle,
    }


def _barrier(box, kind, facility, open_, close, service):
    return {
        "box": box, "kind": kind, "facility": facility, "day": BASE_DAY,
        "open": open_, "close": close, "service": service,
    }


def _cargo(box, vessel="VT"):
    return {"box": box, "vessel": vessel, "appointment": f"T-{box}",
            "day": BASE_DAY, "kind": "普通", "cutoff": 900, "district": "东区"}


def _capacity(vessel, berth_units):
    return [
        {"vessel": vessel, "facility": "船口", "scope": "装卸位",
         "day": BASE_DAY, "open": 430, "close": 930, "units": berth_units},
        {"vessel": vessel, "facility": "公用", "scope": "装卸位",
         "day": BASE_DAY, "open": 430, "close": 930, "units": 2},
        {"vessel": vessel, "facility": "全港", "scope": "短驳班",
         "day": BASE_DAY, "open": 430, "close": 930, "units": 2},
    ]


_TERMS = [
    {"contract": "T1", "party": "码头", "facility": "船口", "rate": 10,
     "reserved": 300, "count_from": "船侧交接", "count_to": "短驳接手", "free": 30},
    {"contract": "T2", "party": "末端接驳", "facility": "全港", "rate": 8,
     "reserved": 200, "count_from": "放行", "count_to": "交付", "free": 20},
]
_REMEDIES = [
    {"remedy": "r1", "facility": "船口", "box": "BW", "gain": 20, "cost": 120},
    {"remedy": "r2", "facility": "船口", "box": "BC", "gain": 40, "cost": 200},
]
_ACCEPTANCES = [
    {"acceptance": "g1", "contract": "T1", "box": "BW", "remedy": "r1",
     "amount": 120, "state": "已确认", "day": "2026-07-02"},
    {"acceptance": "g2", "contract": "T1", "box": "BC", "remedy": "r2",
     "amount": 200, "state": "待调整", "day": "2026-07-03"},
]


def _wrap(cargo, events, barriers, capacity, vessel="VT"):
    return {
        "cargo": cargo, "events": events, "barriers": barriers,
        "capacity": capacity, "terms": copy.deepcopy(_TERMS),
        "remedies": copy.deepcopy(_REMEDIES),
        "acceptances": copy.deepcopy(_ACCEPTANCES),
        "vessel": vessel,
    }


def window_fixture() -> dict:
    """单箱 BW：短驳窗起点 540 绑定交付，放松窗口可减 30 分钟。"""
    box = "BW"
    events = [
        _event(f"{box}-0", box, "船侧交接", 440, 460),
        _event(f"{box}-1", box, "放行", 470, 490),
        _event(f"{box}-2", box, "短驳接手", 470, 490, vehicle="TR7"),
        _event(f"{box}-3", box, "交付", 500, 620),
    ]
    barriers = [
        _barrier(box, "开放泊位", "船口", 430, 700, 15),
        _barrier(box, "堆场复核", "公用", 470, 760, 20),
        # 短驳装运 30 分钟，窗起点 540 -> 最早交付 570；放松到 510 -> 540
        _barrier(box, "短驳班组", "全港", 540, 700, 30),
    ]
    return _wrap([_cargo(box)], events, barriers, _capacity("VT", 2))


def capacity_fixture() -> dict:
    """BA 早到、BB 晚到且证据相同结构；泊位窗只容两个开始时刻，单名额下 BB
    确定性等待 15 分钟，且泊位迟延经 接手->装运->交付 链条传导，容量放松可独立减时。"""
    events, barriers, cargo = [], [], []
    lower = {"BA": 430, "BB": 445}  # BB 船侧交接证据下界晚到，必然后靠
    for box in ("BA", "BB"):
        cargo.append(_cargo(box))
        events += [
            _event(f"{box}-0", box, "船侧交接", lower[box], 480),
            _event(f"{box}-1", box, "放行", 430, 500),
            # 接手上界放开，由物理顺序跟随船侧；下游短驳窗承接
            _event(f"{box}-2", box, "短驳接手", 430, 500, vehicle=f"TR-{box}"),
            # 交付证据下界放早：政策交付由泊位时刻经链条决定
            _event(f"{box}-3", box, "交付", 460, 660),
        ]
        barriers += [
            # start ∈ {430, 445}；单名额时 BA=430、BB=445，后者等待 15
            _barrier(box, "开放泊位", "船口", 430, 460, 15),
            _barrier(box, "堆场复核", "公用", 430, 760, 20),
            # 短驳窗不卡上游：贴住接手上界，泊位改善可传导到交付
            _barrier(box, "短驳班组", "全港", 430, 700, 30),
        ]
    ds = _wrap(cargo, events, barriers, _capacity("VT", 1))
    ds["ordered_handoff_boxes"] = {"BA", "BB"}
    return ds


def joint_fixture(bj_window_open: int = 430) -> dict:
    """箱 BJ 的泊位窗起点与单一名额是否同时绑定取决于 ``bj_window_open``：

    * ``bj_window_open=430``：只有名额绑定（容量放松即可减时）；
    * ``bj_window_open=445``：窗口与名额同时绑定，单独放松任一项都不减时，
      只有联合放松才能提前（仅联合范围，禁止单方认领）。
    """
    cargo = [_cargo("BA"), _cargo("BJ")]
    events = []
    v0_lo = {"BA": 430, "BJ": 445}  # BJ 船侧交接证据下界晚到，确定性后靠
    for box in ("BA", "BJ"):
        events += [
            _event(f"{box}-0", box, "船侧交接", v0_lo[box], 480),
            _event(f"{box}-1", box, "放行", 430, 500),
            _event(f"{box}-2", box, "短驳接手", 430, 500, vehicle=f"TR-{box}"),
            # 交付证据下界放早：政策交付由泊位时刻经链条决定
            _event(f"{box}-3", box, "交付", 460, 660),
        ]
    barriers = [
        # BA 窗可 430 或 445 开始；单名额下 BA 先占 430-445
        _barrier("BA", "开放泊位", "船口", 430, 475, 15),
        _barrier("BA", "堆场复核", "公用", 430, 760, 20),
        _barrier("BA", "短驳班组", "全港", 430, 700, 30),
        # BJ 窗起点参数化；基准被 BA 名额挤到 445
        _barrier("BJ", "开放泊位", "船口", bj_window_open, 475, 15),
        _barrier("BJ", "堆场复核", "公用", 430, 760, 20),
        _barrier("BJ", "短驳班组", "全港", 430, 700, 30),
    ]
    ds = _wrap(cargo, events, barriers, _capacity("VT", 1))
    ds["ordered_handoff_boxes"] = {"BA", "BJ"}
    return ds


def infeasible_fixture() -> dict:
    """箱 BX：船侧交接不早于 560，但泊位窗 430-500 必须覆盖该交接 -> 无可行箱程。"""
    box = "BX"
    events = [
        _event(f"{box}-0", box, "船侧交接", 560, 580),
        _event(f"{box}-1", box, "放行", 600, 620),
        _event(f"{box}-2", box, "短驳接手", 580, 600),
        _event(f"{box}-3", box, "交付", 650, 680),
    ]
    barriers = [
        _barrier(box, "开放泊位", "船口", 430, 500, 15),
        _barrier(box, "堆场复核", "公用", 480, 760, 20),
        _barrier(box, "短驳班组", "全港", 560, 700, 30),
    ]
    return _wrap([_cargo(box, vessel="VX")], events, barriers,
                 _capacity("VX", 1), vessel="VX")


def write_fixture(tmp_path: Path, dataset: dict) -> Path:
    data_dir = Path(tmp_path) / "fixture_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    ordered = dataset.get("ordered_handoff_boxes", [])
    write_json(data_dir / "ordered_handoff.json",
               {"ordered_handoff_boxes": sorted(ordered)})
    for name in ("cargo", "events", "barriers", "capacity", "terms",
                 "remedies", "acceptances"):
        write_json(data_dir / f"{name}.json", dataset[name])
    return data_dir
