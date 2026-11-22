"""合同补救的反事实收益与确认单防护。

同一批箱、同一组运行条件下，对原调度（baseline）、每个补救单项、以及作用于同箱的
补救组合分别重解整船联合调度。组合收益**按真实箱程计算**：搬运位释放后可能被别箱
占用，或被已预告的船侧截单吃掉，这些效应由联合模型自然给出，不做本地节省相加。

收益归属规则：

* 单设施、且归因证据可明确归属于该设施的减时，才可在明示假设下分给对应合同方；
* 仅联合范围（需多项放松同时成立）的份额标记为不可单方分配，留空不硬填；
* 临时补助只允许用于：已有保留额度（``terms.reserved``）**且**存在可核实减时的项目；
* 同一箱/班组被多个补救当作收益来源时，确认单按 (箱, 补救) 去重并拦截重复认领；
* 已书面确认的额度不因新测算自动转付；"待调整"项目冻结，须双方确认调整。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import FACILITY_PARTY
from .attribution import SERVICE_FACILITY
from .scheduler import RelaxSpec, solve_fleet

SCENARIO_BASELINE = "baseline"
STATE_CONFIRMED = "已确认"
STATE_PENDING = "待调整"


@dataclass
class ScenarioResult:
    scenario: str
    remedies: list[str]
    label: str
    deliveries: dict[str, int]
    blocked: dict[str, str]
    total_delivery: int


@dataclass
class RemedyEvaluation:
    remedy: str
    facility: str
    box: str
    party: str | None
    requested_gain: int
    requested_cost: float
    verifiable_minutes_lo: int       # 联合可行域下可核实减时的下界（保守口径）
    verifiable_minutes_value: int    # 政策箱程口径减时
    verifiable_minutes_hi: int       # 减时上界
    value_lo: float
    value_hi: float
    allocable: bool                  # 是否满足单方分配的全部假设
    allocation_note: str
    eligible_for_subsidy: bool
    eligibility_note: str
    net_value_lo: float              # 保守收益相对请求额
    related_contract: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _scenario_spec(ds, remedy_ids: list[str]) -> RelaxSpec:
    by_id = {r["remedy"]: r for r in ds.remedies}
    shift: dict[tuple[str, str], int] = {}
    for rid in remedy_ids:
        row = by_id[rid]
        facility = row["facility"]
        service = next(
            (k for k, f in SERVICE_FACILITY.items() if f == facility), None
        )
        if service is None:
            # 专线等无对应箱侧服务键的设施：记为信息性补救，不进入调度放松
            continue
        shift[(row["box"], service)] = max(
            shift.get((row["box"], service), 0), row["gain"]
        )
    return RelaxSpec(window_shift=shift)


def _contract_for_facility(ds, facility: str) -> str | None:
    for term in ds.terms:
        if term["facility"] == facility:
            return term["contract"]
    return None


def evaluate_scenarios(ds) -> dict:
    """生成 baseline/单项/组合情景并逐一重解。"""
    remedies = ds.remedies
    by_id = {r["remedy"]: r for r in remedies}

    scenario_defs: list[tuple[str, list[str], str]] = [
        (SCENARIO_BASELINE, [], "原调度")
    ]
    for row in remedies:
        scenario_defs.append((row["remedy"], [row["remedy"]], f"仅 {row['remedy']}"))
    # 同箱组合
    boxes: dict[str, list[str]] = {}
    for row in remedies:
        boxes.setdefault(row["box"], []).append(row["remedy"])
    for box, ids in boxes.items():
        if len(ids) > 1:
            name = "combo:" + "+".join(ids)
            scenario_defs.append((name, ids, f"组合 {name}"))

    results: list[ScenarioResult] = []
    fleet_by_scenario: dict[str, dict] = {}
    for name, ids, label in scenario_defs:
        spec = _scenario_spec(ds, ids)
        fleet = solve_fleet(ds, spec, with_ranges=False)
        fleet_by_scenario[name] = fleet
        deliveries: dict[str, int] = {}
        blocked: dict[str, str] = {}
        total = 0
        for vessel_result in fleet.values():
            for plan in vessel_result["plans"]:
                if plan.feasible:
                    deliveries[plan.box] = plan.delivery()
                    total += plan.delivery()
                else:
                    blocked[plan.box] = plan.blocked_reason or "不可行"
        results.append(ScenarioResult(
            scenario=name, remedies=ids, label=label,
            deliveries=deliveries, blocked=blocked, total_delivery=total,
        ))

    base = next(r for r in results if r.scenario == SCENARIO_BASELINE)
    evaluations = _evaluate_remedies(ds, results, base)
    acceptances = _acceptance_report(ds, evaluations)
    return {
        "scenarios": [asdict(r) for r in results],
        "remedies": [e.to_dict() for e in evaluations],
        "acceptances": acceptances,
    }


def _evaluate_remedies(ds, scenarios: list[ScenarioResult],
                       base: ScenarioResult) -> list[RemedyEvaluation]:
    by_scenario = {s.scenario: s for s in scenarios}
    evals: list[RemedyEvaluation] = []
    confirmed_pairs = {
        (a["box"], a["remedy"]): a for a in ds.acceptances
    }

    for row in ds.remedies:
        rid, box, facility = row["remedy"], row["box"], row["facility"]
        contract = _contract_for_facility(ds, facility)
        party = FACILITY_PARTY.get(facility)
        single = by_scenario.get(rid)
        d0 = base.deliveries.get(box)
        d1 = single.deliveries.get(box) if single else None

        if d0 is None or d1 is None:
            minutes_lo = minutes_val = minutes_hi = 0
            note = "箱程不可行或情景无解，无法核实减时"
            allocable = False
        else:
            minutes_val = max(0, d0 - d1)
            # 保守/上界口径：单项情景与组合情景共同佐证
            combo_gains = [
                max(0, d0 - s.deliveries[box])
                for s in scenarios
                if rid in s.remedies and box in s.deliveries
            ]
            minutes_lo = min(combo_gains) if combo_gains else minutes_val
            minutes_hi = max(combo_gains) if combo_gains else minutes_val
            if minutes_val == 0:
                note = "反事实重排未省出交付时间（窗口被后续环节/截单吸收）"
                allocable = False
            elif facility not in FACILITY_PARTY:
                note = "设施无对应合同方（如专线），减时可核实但无合同主体可分配"
                allocable = False
            else:
                note = (
                    "假设：该设施窗口放松是减时唯一边际原因，且同设施共享名额"
                    "未被其他箱占用；若组合情景增益高于单项，则差额属仅联合范围"
                )
                allocable = minutes_val > 0

        rate = ds.term(contract)["rate"] if contract else 0
        value_lo = round(minutes_lo * rate, 2)
        value_hi = round(minutes_hi * rate, 2)
        existing = confirmed_pairs.get((box, rid))
        if contract is None:
            eligible = False
            elig_note = "无对应合同与保留额度，不可使用临时补助"
        elif minutes_lo <= 0:
            eligible = False
            elig_note = "无可核实减时，临时补助不成立"
        else:
            headroom = _reserve_headroom(ds, contract)
            eligible = headroom > 0
            elig_note = (
                f"合同 {contract} 保留额度剩余 {headroom:.0f} 元，"
                + ("可在额度内申请" if eligible else "保留额度已被书面确认占满")
            )
        if existing and existing["state"] == STATE_PENDING:
            elig_note += "；存在待调整确认单，额度冻结，须双方确认后方可变动"

        evals.append(RemedyEvaluation(
            remedy=rid, facility=facility, box=box, party=party,
            requested_gain=row["gain"], requested_cost=row["cost"],
            verifiable_minutes_lo=minutes_lo,
            verifiable_minutes_value=minutes_val,
            verifiable_minutes_hi=minutes_hi,
            value_lo=value_lo, value_hi=value_hi,
            allocable=allocable, allocation_note=note,
            eligible_for_subsidy=eligible, eligibility_note=elig_note,
            net_value_lo=round(value_lo - row["cost"], 2),
            related_contract=contract,
        ))
    return evals


def _reserve_headroom(ds, contract: str) -> float:
    term = ds.term(contract)
    committed = sum(
        a["amount"] for a in ds.acceptances
        if a["contract"] == contract and a["state"] == STATE_CONFIRMED
    )
    pending = sum(
        a["amount"] for a in ds.acceptances
        if a["contract"] == contract and a["state"] == STATE_PENDING
    )
    # 待调整额度同样冻结，不能再被新申请占用
    return round(term["reserved"] - committed - pending, 2)


def _acceptance_report(ds, evaluations: list[RemedyEvaluation]) -> list[dict]:
    eval_index = {(e.box, e.remedy): e for e in evaluations}
    seen: set[tuple[str, str]] = set()
    report: list[dict] = []
    for acc in ds.acceptances:
        key = (acc["box"], acc["remedy"])
        duplicate = key in seen
        seen.add(key)
        ev = eval_index.get(key)
        flags: list[str] = []
        if duplicate:
            flags.append("重复认领：同一箱同一补救已出现确认单，拦截重复支付")
        if acc["state"] == STATE_PENDING:
            flags.append("待调整：已承诺额度冻结，新测算不得自动转付，须双方书面确认")
        if ev is not None and acc["state"] == STATE_CONFIRMED:
            if ev.verifiable_minutes_lo <= 0:
                flags.append("证据复核未发现可核实减时，需重新核对确认依据")
            if ev.related_contract and acc["contract"] != ev.related_contract:
                flags.append("确认单合同与补救设施对应合同不一致")
        headroom = _reserve_headroom(ds, acc["contract"])
        report.append({
            "acceptance": acc["acceptance"],
            "contract": acc["contract"],
            "box": acc["box"],
            "remedy": acc["remedy"],
            "amount": acc["amount"],
            "state": acc["state"],
            "day": acc["day"],
            "reserve_headroom_after": headroom,
            "flags": flags,
            "verifiable_minutes_lo": ev.verifiable_minutes_lo if ev else None,
            "verifiable_minutes_hi": ev.verifiable_minutes_hi if ev else None,
        })

    # 反向检查：多个补救把同一箱当收益来源
    by_box: dict[str, list[str]] = {}
    for acc in ds.acceptances:
        by_box.setdefault(acc["box"], []).append(acc["acceptance"])
    for box, acc_ids in by_box.items():
        if len(acc_ids) > 1:
            for item in report:
                if item["box"] == box:
                    item["flags"].append(
                        f"同一箱 {box} 被 {len(acc_ids)} 张确认单计为收益来源，"
                        "合并结算时只允许按反事实增益认领一次"
                    )
    return report
