"""合同可计费窗口、免费时间与争议差额。

合同（``terms.json``）以 ``count_from``/``count_to`` 两个交接节点界定可计费窗口，
扣除 ``free`` 分钟免费等待后按 ``rate`` 计费。交接时刻只有区间证据，因此每个窗口
都给出**联合可行包络**下的时长区间 [lo, hi]：

* 按下界计 -> 对签约方最有利的合法计费；
* 按上界计 -> 对港区最有利的合法计费；
* 两者之差即本账期的**争议差额**，不能把任何一方的单点主张直接当账。

可行箱程缺失的箱，其计价项目整体拦下并保留原因，不进入合计；其他箱照常计费。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .scheduler import RelaxSpec, VesselModel, _build_box_models


@dataclass
class BillingLine:
    contract: str
    party: str
    facility: str
    box: str
    vessel: str
    day: str
    count_from: str
    count_to: str
    free: int
    rate: float
    window_lo: int                 # 联合可行域内窗口最短分钟
    window_hi: int                 # 联合可行域内窗口最长分钟
    window_value: int              # 政策箱程下的窗口分钟
    billable_lo: int
    billable_hi: int
    billable_value: int
    amount_lo: float
    amount_hi: float
    amount_value: float
    dispute_amount: float          # 上界-下界，证据未能区分的争议额
    status: str
    blocked_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TermSummary:
    contract: str
    party: str
    facility: str
    rate: float
    reserved: float
    amount_lo: float
    amount_hi: float
    amount_value: float
    dispute_amount: float
    boxes: list[str] = field(default_factory=list)
    blocked_boxes: list[str] = field(default_factory=list)
    lines: list[BillingLine] = field(default_factory=list)


def term_applies(term: dict, box_barriers: dict) -> bool:
    """合同按 facility 对齐箱侧设施；全港合同适用于全部箱。"""
    if term["facility"] == "全港":
        return True
    return any(b["facility"] == term["facility"] for b in box_barriers.values())


def _window_envelope(ds, vessel: str, box_ids: list[str],
                     from_node: str, to_node: str) -> dict[str, tuple[int, int, int]]:
    """对整船重解一次，返回各箱窗口时长 (lo, hi, policy_value)。"""
    models, _ = _build_box_models(ds, vessel, box_ids, RelaxSpec())
    day = ds.cargo_by_box()[box_ids[0]]["day"]
    capacity = ds.vessel_capacity(vessel, day)
    model = VesselModel(vessel, day, models, capacity, RelaxSpec()).build()
    base = model.policy_solution()
    out: dict[str, tuple[int, int, int]] = {}
    for bm in models:
        i_from = bm.vars[f"t:{bm.box}:{from_node}"]
        i_to = bm.vars[f"t:{bm.box}:{to_node}"]
        rng = model.extreme({i_to: 1.0, i_from: -1.0})
        if rng is None:
            continue
        lo_v, hi_v = rng
        value = int(base[i_to] - base[i_from])
        out[bm.box] = (max(0, lo_v), max(0, hi_v), max(0, value))
    return out


def build_billing(ds, fleet: dict) -> dict:
    """按合同批次产出逐箱计费行与合同汇总。"""
    cargo_index = ds.cargo_by_box()
    # 预计算每个船次的三个合同窗口包络
    envelopes: dict[tuple[str, str, str], dict] = {}
    plan_index: dict[str, object] = {}
    for vessel, result in fleet.items():
        feasible = [p.box for p in result["plans"] if p.feasible]
        plan_index.update({p.box: p for p in result["plans"]})
        for term in ds.terms:
            key = (vessel, term["count_from"], term["count_to"])
            if feasible:
                envelopes[key] = _window_envelope(
                    ds, vessel, feasible, term["count_from"], term["count_to"]
                )
            else:
                envelopes[key] = {}

    summaries: list[TermSummary] = []
    for term in ds.terms:
        summary = TermSummary(
            contract=term["contract"], party=term["party"],
            facility=term["facility"], rate=term["rate"],
            reserved=term["reserved"],
            amount_lo=0.0, amount_hi=0.0, amount_value=0.0, dispute_amount=0.0,
        )
        for cargo in ds.cargo:
            box = cargo["box"]
            barriers = ds.box_barriers(box)
            if not term_applies(term, barriers):
                continue
            plan = plan_index[box]
            if not plan.feasible:
                summary.blocked_boxes.append(box)
                summary.lines.append(BillingLine(
                    contract=term["contract"], party=term["party"],
                    facility=term["facility"], box=box, vessel=cargo["vessel"],
                    day=cargo["day"], count_from=term["count_from"],
                    count_to=term["count_to"], free=term["free"],
                    rate=term["rate"],
                    window_lo=0, window_hi=0, window_value=0,
                    billable_lo=0, billable_hi=0, billable_value=0,
                    amount_lo=0.0, amount_hi=0.0, amount_value=0.0,
                    dispute_amount=0.0, status="拦下",
                    blocked_reason=plan.blocked_reason,
                ))
                continue

            env = envelopes[(cargo["vessel"], term["count_from"], term["count_to"])]
            win_lo, win_hi, win_val = env[box]
            bill_lo = max(0, win_lo - term["free"])
            bill_hi = max(0, win_hi - term["free"])
            bill_val = max(0, win_val - term["free"])
            line = BillingLine(
                contract=term["contract"], party=term["party"],
                facility=term["facility"], box=box, vessel=cargo["vessel"],
                day=cargo["day"], count_from=term["count_from"],
                count_to=term["count_to"], free=term["free"],
                rate=term["rate"],
                window_lo=win_lo, window_hi=win_hi, window_value=win_val,
                billable_lo=bill_lo, billable_hi=bill_hi, billable_value=bill_val,
                amount_lo=round(bill_lo * term["rate"], 2),
                amount_hi=round(bill_hi * term["rate"], 2),
                amount_value=round(bill_val * term["rate"], 2),
                dispute_amount=round((bill_hi - bill_lo) * term["rate"], 2),
                status="可计费",
            )
            summary.lines.append(line)
            summary.boxes.append(box)
            summary.amount_lo = round(summary.amount_lo + line.amount_lo, 2)
            summary.amount_hi = round(summary.amount_hi + line.amount_hi, 2)
            summary.amount_value = round(summary.amount_value + line.amount_value, 2)
            summary.dispute_amount = round(
                summary.dispute_amount + line.dispute_amount, 2
            )
        summaries.append(summary)

    return {
        "terms": [
            {
                **asdict(s),
                "lines": [line.to_dict() for line in s.lines],
            }
            for s in summaries
        ],
        "grand": {
            "amount_lo": round(sum(s.amount_lo for s in summaries), 2),
            "amount_hi": round(sum(s.amount_hi for s in summaries), 2),
            "amount_value": round(sum(s.amount_value for s in summaries), 2),
            "dispute_amount": round(sum(s.dispute_amount for s in summaries), 2),
        },
    }
