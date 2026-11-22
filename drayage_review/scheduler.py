"""可行箱程调度引擎。

一个箱程由 4 个不确定交接时刻（区间变量）和 3 段专占设施服务组成：

* 船口开放泊位（berth）：服务窗必须**覆盖**船侧交接，且在船侧截单前完成；
* 堆场复核位（yard）：服务窗必须**覆盖**放行；
* 短驳班组（dray）：船侧交接与放行均完成后才可开始装运，装运完成不晚于交付。

共享设施同一时刻最多 ``units`` 个箱程占用（按船次/运行日的 capacity 表）。
各设施窗口名额的专占性用轨道分配 + 轨道内排序的混合整数模型表达。

引擎不挑"对某来源最有利的一条时间线"，而是：

1. 求一条联合可行的政策箱程（最小化总交付时刻，再最小化总服务开始时刻）；
2. 对每个时刻变量求可行域内的最小值/最大值，形成**联合可行包络**，
   供合同计时与归因使用；
3. 时序证据冲突到无可行箱程时，逐箱定位并隔离，受影响计价项目拦下，
   同船次其他箱照常复盘。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

# 大 M：窗口都在当日 0~1440 分钟内，1500 足以放松任何一对排序约束
BIG_M = 1500
# 归因反事实中"解除箱侧窗口起点"使用的提前幅度（大于数据中任何实际等待）
RELAX_BIG = 720
# 目标函数权重：交付时刻优先于服务开始时刻
W_DELIVERY = 100_000
W_START = 1
HORIZON_LO, HORIZON_HI = 0, 1440

SERVICE_KEYS = ("berth", "yard", "dray")
# 服务覆盖/跟随的交接节点
SERVICE_COVERS = {"berth": "船侧交接", "yard": "放行"}
DRAY_AFTER = ("放行", "短驳接手")


class InfeasibleBox(Exception):
    """单箱证据本身无法构成可行箱程。"""


@dataclass
class RelaxSpec:
    """反事实情景对约束的修改。

    * ``window_shift``: (box, service) -> 窗口起点提前分钟（补救 gain 或归因放松）；
    * ``relax_capacity_facilities``: 解除共享名额限制的设施集合；
    * ``drop_boxes``: 不参与排程的箱（冲突隔离用）。
    """

    window_shift: dict[tuple[str, str], int] = field(default_factory=dict)
    relax_capacity_facilities: set[str] = field(default_factory=set)
    drop_boxes: set[str] = field(default_factory=set)


@dataclass
class VariableInfo:
    name: str
    index: int
    integer: bool


@dataclass
class BoxModel:
    box: str
    milestones: dict[str, Any]          # kind -> event row
    services: dict[str, dict[str, Any]]  # key -> barrier row
    cutoff: int
    ordered_handoff: bool = False       # 放行严格先于短驳接手的物理顺序链
    vars: dict[str, int] = field(default_factory=dict)


@dataclass
class ServiceResult:
    facility: str
    kind: str
    open: int
    close: int
    service: int
    start: int
    end: int
    start_min: int
    start_max: int


@dataclass
class MilestoneResult:
    kind: str
    lo: int
    hi: int
    value: int
    min: int
    max: int
    revision: int
    available: str
    vehicle: str | None


@dataclass
class WindowResult:
    name: str
    start_node: str
    end_node: str
    elapsed_lo: int
    elapsed_hi: int
    elapsed_value: int


@dataclass
class BoxPlan:
    box: str
    vessel: str
    day: str
    feasible: bool
    milestones: dict[str, MilestoneResult] = field(default_factory=dict)
    services: dict[str, ServiceResult] = field(default_factory=dict)
    windows: dict[str, WindowResult] = field(default_factory=dict)
    blocked_reason: str | None = None

    def delivery(self) -> int:
        return self.milestones["交付"].value


class VesselModel:
    """单船次（单日）混合整数模型，可换目标重复求解。"""

    def __init__(self, vessel: str, day: str, boxes: list[BoxModel],
                 capacity: dict[str, dict[str, Any]],
                 relax: RelaxSpec | None = None) -> None:
        self.vessel = vessel
        self.day = day
        self.boxes = [b for b in boxes if b.box not in (relax.drop_boxes if relax else set())]
        self.capacity = capacity
        self.relax = relax or RelaxSpec()
        self._built = False
        self._names: dict[str, int] = {}
        self._integer: list[int] = []
        self._binary: list[int] = []
        self._lb: list[float] = []
        self._ub: list[float] = []
        self._rows: list[np.ndarray] = []
        self._lbs: list[float] = []
        self._ubs: list[float] = []
        self._cap_pools: dict[str, list[tuple[BoxModel, str]]] = {}
        self._cap_assign: dict[tuple[str, int, int], int] = {}
        self._cap_order: dict[tuple[str, int, int, int], int] = {}
        self._effective_open: dict[tuple[str, str], int] = {}
        self.n = 0

    # ---- 建模基础 ------------------------------------------------------------
    def _add_var(self, name: str, lb: float, ub: float, integer: bool,
                 binary: bool = False) -> int:
        idx = self.n
        self.n += 1
        self._names[name] = idx
        self._lb.append(lb)
        self._ub.append(ub)
        if binary:
            self._binary.append(idx)
        elif integer:
            self._integer.append(idx)
        return idx

    def _constraint(self, coeffs: dict[int, float], lb: float, ub: float) -> None:
        row = np.zeros(self.n)
        for idx, coef in coeffs.items():
            row[idx] = coef
        self._rows.append(row)
        self._lbs.append(lb)
        self._ubs.append(ub)

    def _le(self, coeffs: dict[int, float], rhs: float) -> None:
        self._constraint(coeffs, -np.inf, rhs)

    def _eq(self, coeffs: dict[int, float], rhs: float) -> None:
        self._constraint(coeffs, rhs, rhs)

    def build(self) -> "VesselModel":
        if self._built:
            return self
        for bm in self.boxes:
            b = bm.box
            # 交接时刻：区间整数变量，界由当前有效证据给定
            for kind, ev in bm.milestones.items():
                bm.vars[f"t:{b}:{kind}"] = self._add_var(
                    f"t:{b}:{kind}", ev["lo"], ev["hi"], integer=True
                )
            # 三段服务的开始时刻；结束 = 开始 + service。
            # 窗口上下界统一以线性约束表达（这样窗口放松/反事实才能生效），
            # 变量自身只保留当日地平线。
            for key, barrier in bm.services.items():
                shift = self.relax.window_shift.get((b, key), 0)
                self._effective_open[(b, key)] = barrier["open"] - shift
                bm.vars[f"s:{b}:{key}"] = self._add_var(
                    f"s:{b}:{key}", HORIZON_LO, HORIZON_HI, integer=True
                )
        # 先声明全部变量（含容量轨道/排序二元变量），再统一追加等宽约束行
        self._build_capacity_vars()
        self._build_structural()
        self._build_capacity_constraints()
        self._built = True
        return self

    def _t(self, bm: BoxModel, kind: str) -> int:
        return bm.vars[f"t:{bm.box}:{kind}"]

    def _s(self, bm: BoxModel, key: str) -> int:
        return bm.vars[f"s:{bm.box}:{key}"]

    def _build_structural(self) -> None:
        for bm in self.boxes:
            b = bm.box
            # 服务窗下界（含反事实放松后的起点）
            for key in SERVICE_KEYS:
                s = self._s(bm, key)
                self._le({s: -1}, -self._effective_open[(b, key)])
            t_v = self._t(bm, "船侧交接")
            t_r = self._t(bm, "放行")
            t_h = self._t(bm, "短驳接手")
            t_d = self._t(bm, "交付")
            # 物理顺序：船侧交接先于放行与短驳接手。
            # 放行与短驳接手之间默认不设全局顺序——班次界限允许接手早于放行；
            # 仅当证据本身保证顺序时才加 R<=H，使上游迟延可沿链条传导。
            self._le({t_v: 1, t_r: -1}, 0)
            self._le({t_v: 1, t_h: -1}, 0)
            if bm.ordered_handoff:
                self._le({t_r: 1, t_h: -1}, 0)

            # 船口泊位覆盖船侧交接：s <= t_V <= s+service；
            # 整段服务必须落在 barrier 窗口内，且在船侧截单前完成
            s_b = self._s(bm, "berth")
            dur_b = bm.services["berth"]["service"]
            self._le({s_b: 1, t_v: -1}, 0)
            self._le({t_v: 1, s_b: -1}, dur_b)
            self._le({s_b: 1}, bm.services["berth"]["close"] - dur_b)
            self._le({s_b: 1}, bm.cutoff - dur_b)

            # 堆场复核覆盖放行：s <= t_R <= s+service，整段落在窗口内
            s_y = self._s(bm, "yard")
            dur_y = bm.services["yard"]["service"]
            self._le({s_y: 1, t_r: -1}, 0)
            self._le({t_r: 1, s_y: -1}, dur_y)
            self._le({s_y: 1}, bm.services["yard"]["close"] - dur_y)

            # 短驳装运在两次交接之后，整段落在窗口内，完成不晚于交付
            s_d = self._s(bm, "dray")
            dur_d = bm.services["dray"]["service"]
            self._le({t_r: 1, s_d: -1}, 0)
            self._le({t_h: 1, s_d: -1}, 0)
            self._le({s_d: 1}, bm.services["dray"]["close"] - dur_d)
            self._le({s_d: 1, t_d: -1}, -dur_d)

    def _capacity_pools(self) -> dict[str, list[tuple[BoxModel, str]]]:
        pools: dict[str, list[tuple[BoxModel, str]]] = {}
        for bm in self.boxes:
            for key, barrier in bm.services.items():
                pools.setdefault(barrier["facility"], []).append((bm, key))
        return pools

    def _build_capacity_vars(self) -> None:
        """仅声明轨道分配与同轨排序所需的二元变量。"""
        self._cap_pools = self._capacity_pools()
        self._cap_assign: dict[tuple[str, int, int], int] = {}
        self._cap_order: dict[tuple[str, int, int, int], int] = {}
        for facility, members in self._cap_pools.items():
            cap = self.capacity.get(facility)
            units = cap["units"] if cap else 1
            if facility in self.relax.relax_capacity_facilities:
                units = max(units, len(members))
            if len(members) <= 1 or units >= len(members):
                continue
            for k in range(len(members)):
                for r in range(units):
                    bm, key = members[k]
                    self._cap_assign[(facility, k, r)] = self._add_var(
                        f"a:{facility}:{bm.box}:{key}:{r}", 0, 1,
                        integer=False, binary=True,
                    )
            for k in range(len(members)):
                for j in range(k + 1, len(members)):
                    for r in range(units):
                        self._cap_order[(facility, k, j, r)] = self._add_var(
                            f"z:{facility}:{k}:{j}:{r}", 0, 1,
                            integer=False, binary=True,
                        )

    def _build_capacity_constraints(self) -> None:
        """基于已声明的二元变量追加轨道分配与不重叠约束。"""
        for facility, members in self._cap_pools.items():
            cap = self.capacity.get(facility)
            units = cap["units"] if cap else 1
            if facility in self.relax.relax_capacity_facilities:
                units = max(units, len(members))
            if len(members) <= 1 or units >= len(members):
                continue
            for k in range(len(members)):
                self._eq(
                    {self._cap_assign[(facility, k, r)]: 1 for r in range(units)},
                    1,
                )
            for k in range(len(members)):
                for j in range(k + 1, len(members)):
                    bm_k, key_k = members[k]
                    bm_j, key_j = members[j]
                    s_k = self._s(bm_k, key_k)
                    s_j = self._s(bm_j, key_j)
                    d_k = bm_k.services[key_k]["service"]
                    d_j = bm_j.services[key_j]["service"]
                    for r in range(units):
                        z = self._cap_order[(facility, k, j, r)]
                        a_k = self._cap_assign[(facility, k, r)]
                        a_j = self._cap_assign[(facility, j, r)]
                        # 仅当两服务同占一条轨道（a_k=a_j=1）时排序才生效。
                        # 令 gate = M*(2-a_k-a_j)，同轨时为 0，否则为 M/2M 放松：
                        # z=1 -> k 在 j 前；z=0 -> j 在 k 前。
                        # s_k + d_k <= s_j + M*(1-z) + gate
                        self._le(
                            {s_k: 1, s_j: -1, z: BIG_M, a_k: BIG_M, a_j: BIG_M},
                            -d_k + BIG_M * 3,
                        )
                        # s_j + d_j <= s_k + M*z + gate
                        self._le(
                            {s_j: 1, s_k: -1, z: BIG_M, a_k: BIG_M, a_j: BIG_M},
                            -d_j + BIG_M * 2,
                        )

    # ---- 求解 ----------------------------------------------------------------
    def _solve(self, objective: dict[int, float] | None,
               sense: str = "min") -> np.ndarray | None:
        c = np.zeros(self.n)
        if objective:
            for idx, w in objective.items():
                c[idx] = w if sense == "min" else -w
        integrality = np.zeros(self.n)
        integrality[self._integer] = 1
        integrality[self._binary] = 1
        bounds = Bounds(np.array(self._lb, dtype=float),
                        np.array(self._ub, dtype=float))
        constraints = LinearConstraint(
            np.vstack(self._rows),
            np.array(self._lbs, dtype=float),
            np.array(self._ubs, dtype=float),
        )
        result = milp(
            c=c, integrality=integrality, bounds=bounds,
            constraints=constraints,
            options={"time_limit": 60, "mip_rel_gap": 0},
        )
        if result.status in (2, 3):  # infeasible / unbounded
            return None
        if result.x is None:
            return None
        return np.rint(result.x).astype(int)

    def policy_solution(self) -> np.ndarray | None:
        """政策箱程：先最小化总交付时刻，再按箱序最小化服务开始时刻。

        次目标按箱号顺序给递增的微小权重，保证总交付相同时早到箱优先占用
        共享名额（确定性平局决胜），避免求解器任意排序影响归因口径。
        """
        obj: dict[int, float] = {}
        for rank, bm in enumerate(self.boxes, start=1):
            obj[self._t(bm, "交付")] = W_DELIVERY
            for key in SERVICE_KEYS:
                obj[self._s(bm, key)] = W_START * rank
        return self._solve(obj)

    def range_of(self, var_name: str) -> tuple[int, int] | None:
        """变量在联合可行域内的最小/最大值。"""
        idx = self._names[var_name]
        return self.extreme({idx: 1.0})

    def extreme(self, coeffs: dict[int, float]) -> tuple[int, int] | None:
        """线性表达式在联合可行域内的最小/最大值（整数化）。"""
        lo = self._solve(coeffs, "min")
        hi = self._solve(coeffs, "max")
        if lo is None or hi is None:
            return None
        lo_v = int(round(sum(w * lo[i] for i, w in coeffs.items())))
        hi_v = int(round(sum(w * hi[i] for i, w in coeffs.items())))
        return lo_v, hi_v


# ---------------------------------------------------------------------------
# 装配与对外入口
# ---------------------------------------------------------------------------
def _build_box_models(ds, vessel: str, box_ids: list[str],
                      relax: RelaxSpec | None) -> tuple[list[BoxModel], list[tuple[str, str]]]:
    cargo_index = ds.cargo_by_box()
    ordered_override = getattr(ds, "ordered_handoff_boxes", set())
    models: list[BoxModel] = []
    blocked: list[tuple[str, str]] = []
    for box in box_ids:
        cargo = cargo_index[box]
        milestones = ds.milestones(box)
        services = ds.box_barriers(box)
        missing = [k for k in ("船侧交接", "放行", "短驳接手", "交付") if k not in milestones]
        missing += [k for k in SERVICE_KEYS if k not in services]
        if missing:
            blocked.append((box, "证据缺失：" + "、".join(missing)))
            continue
        ordered = (
            box in ordered_override
            or milestones["放行"]["hi"] <= milestones["短驳接手"]["lo"]
        )
        models.append(BoxModel(
            box=box,
            milestones=milestones,
            services=services,
            cutoff=cargo["cutoff"],
            # 证据本身保证放行不晚于短驳接手，或业务事实显式声明时启用顺序链；
            # 证据区间重叠（班次界限）的箱程不启用，保留两种时序的可能
            ordered_handoff=ordered,
        ))
    return models, blocked


def _plan_from_solution(model: VesselModel, sol: np.ndarray,
                        with_ranges: bool) -> list[BoxPlan]:
    plans: list[BoxPlan] = []
    for bm in model.boxes:
        plan = BoxPlan(box=bm.box, vessel=model.vessel, day=model.day, feasible=True)
        for kind, ev in bm.milestones.items():
            idx = bm.vars[f"t:{bm.box}:{kind}"]
            if with_ranges:
                rng = model.range_of(f"t:{bm.box}:{kind}")
                lo_v, hi_v = rng if rng else (int(sol[idx]), int(sol[idx]))
            else:
                lo_v = hi_v = int(sol[idx])
            plan.milestones[kind] = MilestoneResult(
                kind=kind, lo=ev["lo"], hi=ev["hi"], value=int(sol[idx]),
                min=lo_v, max=hi_v, revision=ev["revision"],
                available=ev["available"], vehicle=ev.get("vehicle"),
            )
        for key, barrier in bm.services.items():
            idx = bm.vars[f"s:{bm.box}:{key}"]
            if with_ranges:
                rng = model.range_of(f"s:{bm.box}:{key}")
                s_lo, s_hi = rng if rng else (int(sol[idx]), int(sol[idx]))
            else:
                s_lo = s_hi = int(sol[idx])
            start = int(sol[idx])
            plan.services[key] = ServiceResult(
                facility=barrier["facility"], kind=barrier["kind"],
                open=barrier["open"], close=barrier["close"],
                service=barrier["service"], start=start,
                end=start + barrier["service"],
                start_min=s_lo, start_max=s_hi,
            )
        plans.append(plan)
    return plans


def solve_vessel(ds, vessel: str, box_ids: list[str],
                 relax: RelaxSpec | None = None,
                 with_ranges: bool = True) -> dict[str, Any]:
    """求解一个船次的联合可行箱程。

    返回 ``{"plans", "blocked", "status"}``。整船无解时逐箱定位冲突箱并隔离，
    被隔离箱的计价项目由上层拦下，其余箱继续复盘。
    """
    relax = relax or RelaxSpec()
    models, pre_blocked = _build_box_models(ds, vessel, box_ids, relax)
    day = ds.cargo_by_box()[box_ids[0]]["day"]
    capacity = ds.vessel_capacity(vessel, day)
    reason_text = "时序证据冲突：不存在满足全部交接与设施约束的可行箱程"

    def attempt(active: list[BoxModel]) -> np.ndarray | None:
        if not active:
            return np.array([], dtype=int)
        m = VesselModel(vessel, day, active, capacity, relax).build()
        return m.policy_solution()

    blocked: list[tuple[str, str]] = list(pre_blocked)
    active = [m for m in models if m.box not in relax.drop_boxes]
    sol = attempt(active)

    if sol is None:
        # 逐箱定位：剔除该箱后若其余箱恢复可行，该箱为冲突来源
        culprits: list[str] = []
        for bm in list(active):
            rest = [x for x in active if x.box != bm.box]
            if attempt(rest) is not None:
                culprits.append(bm.box)
        # 多箱联合冲突（剔除任何单箱都无解）：确定性地逐只剔除直到恢复
        remaining = [x for x in active if x.box not in culprits]
        while remaining and attempt(remaining) is None:
            victim = remaining.pop(0)
            culprits.append(victim.box)
        for box in culprits:
            blocked.append((box, reason_text))
        active = [m for m in active if m.box not in {c for c, _ in blocked}]
        sol = attempt(active)

    plans: list[BoxPlan] = []
    if sol is not None and active:
        model = VesselModel(vessel, day, active, capacity, relax).build()
        base = model.policy_solution()
        if base is not None:
            plans = _plan_from_solution(model, base, with_ranges)

    blocked_set = {b for b, _ in blocked}
    for box in box_ids:
        if box in blocked_set:
            reason = next(r for b, r in blocked if b == box)
            cargo = ds.cargo_by_box()[box]
            plans.append(BoxPlan(
                box=box, vessel=vessel, day=cargo["day"],
                feasible=False, blocked_reason=reason,
            ))
    plans.sort(key=lambda p: p.box)
    return {
        "vessel": vessel,
        "day": day,
        "plans": plans,
        "blocked": blocked,
        "status": "blocked" if blocked else "ok",
    }


def solve_fleet(ds, relax: RelaxSpec | None = None,
                with_ranges: bool = True) -> dict[str, dict[str, Any]]:
    """逐船次求解全船队（船次之间设施与班组互不共享）。"""
    out = {}
    for vessel, box_ids in ds.boxes_by_vessel().items():
        out[vessel] = solve_vessel(ds, vessel, box_ids, relax, with_ranges)
    return out
