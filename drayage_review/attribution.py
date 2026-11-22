"""迟延归因：边际反事实，而不是给每个来源各挑一条可行时间线。

对每只箱的政策交付时刻 ``D`` 做三类反事实（都在同一联合可行模型上重解）：

* 单独放松该设施的**箱侧窗口起点**（``window``）；
* 单独解除该设施的**共享名额限制**（``capacity``）；
* 两者同时放松（``joint``）。

判定规则：

* 只有窗口或容量之一放松就能减少交付 -> 该部分**明确可归因**；
* 必须两者同时放松才有减少 -> **仅联合范围**，证据不足以在设施/班组之间拆分，
  份额留空，禁止把各方本地记录的节省直接相加；
* 联合放松仍无改善 -> 迟延由交接证据区间本身决定，不归属任何设施方。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import FACILITY_PARTY
from .scheduler import (
    RELAX_BIG,
    BoxModel,
    RelaxSpec,
    VesselModel,
    _build_box_models,
)

# 内部服务键 -> 设施方说明
SERVICE_FACILITY = {
    "berth": "船口",
    "yard": "公用",
    "dray": "全港",
}


@dataclass
class FacilityAttribution:
    facility: str
    service: str
    party: str | None
    baseline_delivery: int
    window_only_delivery: int
    capacity_only_delivery: int
    joint_delivery: int
    window_relief: int          # 单独放松箱侧窗口可明确归因的减时
    capacity_relief: int        # 单独解除共享名额可明确归因的减时
    joint_only_relief: int      # 仅联合放松才出现的减时（不可单方认领）
    max_relief: int             # 该设施反事实减时上界

    @property
    def attributable_relief(self) -> int:
        return self.window_relief + self.capacity_relief

    @property
    def unallocable_relief(self) -> int:
        return self.joint_only_relief


@dataclass
class BoxAttribution:
    box: str
    vessel: str
    feasible: bool
    baseline_delivery: int | None = None
    evidence_earliest_delivery: int | None = None
    evidence_bound_relief: int | None = None
    facilities: list[FacilityAttribution] = field(default_factory=list)
    blocked_reason: str | None = None

    def to_dict(self) -> dict:
        out = asdict(self)
        return out


def _delivery_under(ds, vessel: str, box_ids: list[str],
                    spec: RelaxSpec) -> dict[str, int]:
    """在给定反事实下重解整船，返回各箱政策交付时刻；不可行返回空。"""
    if not box_ids:
        return {}
    models, blocked = _build_box_models(ds, vessel, box_ids, spec)
    if blocked:
        return {}
    day = ds.cargo_by_box()[box_ids[0]]["day"]
    capacity = ds.vessel_capacity(vessel, day)
    model = VesselModel(vessel, day, models, capacity, spec).build()
    sol = model.policy_solution()
    if sol is None:
        return {}
    out = {}
    for bm in models:
        out[bm.box] = int(sol[bm.vars[f"t:{bm.box}:交付"]])
    return out


def _earliest_evidence_delivery(ds, box: str) -> int:
    ms = ds.milestones(box)
    return ms["交付"]["lo"]


def attribute_vessel(ds, vessel_result: dict) -> list[BoxAttribution]:
    vessel = vessel_result["vessel"]
    box_ids = [p.box for p in vessel_result["plans"]]
    feasible_ids = [p.box for p in vessel_result["plans"] if p.feasible]
    baseline = _delivery_under(ds, vessel, feasible_ids, RelaxSpec())

    results: list[BoxAttribution] = []
    for plan in vessel_result["plans"]:
        box = plan.box
        if not plan.feasible:
            results.append(BoxAttribution(
                box=box, vessel=vessel, feasible=False,
                blocked_reason=plan.blocked_reason,
            ))
            continue
        if not feasible_ids:
            results.append(BoxAttribution(
                box=box, vessel=vessel, feasible=False,
                blocked_reason="同船次全部箱程不可行，无法建立反事实基线",
            ))
            continue

        d0 = baseline[box]
        ba = BoxAttribution(
            box=box, vessel=vessel, feasible=True,
            baseline_delivery=d0,
            evidence_earliest_delivery=_earliest_evidence_delivery(ds, box),
        )
        ba.evidence_bound_relief = d0 - ba.evidence_earliest_delivery

        for service, facility in SERVICE_FACILITY.items():
            win = RelaxSpec(window_shift={(box, service): RELAX_BIG})
            cap = RelaxSpec(relax_capacity_facilities={facility})
            joint = RelaxSpec(
                window_shift={(box, service): RELAX_BIG},
                relax_capacity_facilities={facility},
            )
            d_win = _delivery_under(ds, vessel, feasible_ids, win).get(box, d0)
            d_cap = _delivery_under(ds, vessel, feasible_ids, cap).get(box, d0)
            d_joint = _delivery_under(ds, vessel, feasible_ids, joint).get(box, d0)

            window_relief = max(0, d0 - d_win)
            capacity_relief = max(0, d0 - d_cap)
            max_relief = max(0, d0 - d_joint)
            # 联合放松的总改善里，先扣除已能单独明确归因的部分，其余为仅联合范围
            joint_only = max(0, max_relief - window_relief - capacity_relief)

            ba.facilities.append(FacilityAttribution(
                facility=facility,
                service=service,
                party=FACILITY_PARTY.get(facility),
                baseline_delivery=d0,
                window_only_delivery=d_win,
                capacity_only_delivery=d_cap,
                joint_delivery=d_joint,
                window_relief=window_relief,
                capacity_relief=capacity_relief,
                joint_only_relief=joint_only,
                max_relief=max_relief,
            ))
        results.append(ba)
    return results


def attribute_fleet(ds, fleet: dict) -> dict[str, list[BoxAttribution]]:
    return {v: attribute_vessel(ds, r) for v, r in fleet.items()}
