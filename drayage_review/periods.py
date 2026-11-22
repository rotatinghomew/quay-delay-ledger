"""账期滚动：运行快照与差额成因分解。

每次联合复盘生成一份不可变运行快照（manifest + 明细 JSON）。下一次账期复算时，
逐箱、逐合同行对比上一份快照，把差额归入三类成因：

* ``observation`` 观察更完整：新登记的更正/观察改变了交接区间或可行域
  （由 ``available`` 晚于上一 as_of 的记录驱动）；
* ``attribution`` 责任解释变化：证据相同，但边际归因/可分配份额发生变化；
* ``remedy`` 补救实际省时：反事实情景中确有新的可核实减时。

被拦下的计价项目与"待调整"确认单原样结转，不参与自动差额分配。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .io import write_json

CAUSE_OBSERVATION = "observation"
CAUSE_ATTRIBUTION = "attribution"
CAUSE_REMEDY = "remedy"
CAUSE_LABELS = {
    CAUSE_OBSERVATION: "观察更完整（新登记更正/观察）",
    CAUSE_ATTRIBUTION: "责任解释变化（归因/可分配份额调整）",
    CAUSE_REMEDY: "补救实际省出的时间",
}


@dataclass
class RunBundle:
    manifest: dict
    billing: dict
    attribution: dict
    remedies: dict
    fleet: dict


def _box_window_index(billing: dict) -> dict[tuple[str, str], dict]:
    index = {}
    for term in billing["terms"]:
        for line in term["lines"]:
            index[(term["contract"], line["box"])] = line
    return index


def _box_delivery_index(fleet: dict) -> dict[str, dict]:
    out = {}
    for result in fleet.values():
        if isinstance(result, list):
            plans = result
        elif isinstance(result, dict):
            plans = result["plans"] if "plans" in result else result
        else:
            plans = result.plans
        for plan in plans:
            if isinstance(plan, dict):
                out[plan["box"]] = {
                    "feasible": plan["feasible"],
                    "delivery": (
                        plan["milestones"]["交付"]["value"]
                        if plan["feasible"] else None
                    ),
                    "blocked_reason": plan.get("blocked_reason"),
                }
            else:
                out[plan.box] = {
                    "feasible": plan.feasible,
                    "delivery": plan.delivery() if plan.feasible else None,
                    "blocked_reason": plan.blocked_reason,
                }
    return out


def _new_evidence_boxes(previous_manifest: dict, current_manifest: dict,
                        ds) -> set[str]:
    prev_as_of = previous_manifest.get("as_of")
    cur_as_of = current_manifest.get("as_of")
    if not prev_as_of or not cur_as_of:
        return set()
    boxes: set[str] = set()
    for row in ds.events:  # ds 已按当前 as_of 过滤，剩余记录都是当前可见的
        if prev_as_of < row["available"] <= cur_as_of:
            boxes.add(row["box"])
    return boxes


def compare_runs(previous: RunBundle, current: RunBundle, ds) -> dict:
    """对比两次运行，输出逐箱/逐合同行的差额与成因。"""
    prev_lines = _box_window_index(previous.billing)
    cur_lines = _box_window_index(current.billing)
    prev_delivery = _box_delivery_index(previous.fleet)
    cur_delivery = _box_delivery_index(current.fleet)
    evidence_boxes = _new_evidence_boxes(
        previous.manifest, current.manifest, ds
    )
    # 同船次存在新证据的箱：其联合可行域可能经共享名额/班组能力联动改变
    vessel_of_box = {c["box"]: c["vessel"] for c in ds.cargo}
    vessels_with_new_evidence = {
        vessel_of_box[b] for b in evidence_boxes if b in vessel_of_box
    }
    linked_boxes = {
        box for box, vessel in vessel_of_box.items()
        if vessel in vessels_with_new_evidence and box not in evidence_boxes
    }

    line_deltas: list[dict] = []
    for key in sorted(set(prev_lines) | set(cur_lines)):
        old = prev_lines.get(key)
        new = cur_lines.get(key)
        contract, box = key
        if old is None:
            cause = (CAUSE_OBSERVATION if box in evidence_boxes
                     else CAUSE_ATTRIBUTION)
            line_deltas.append({
                "contract": contract, "box": box, "change": "新增计费行",
                "cause": cause, "cause_label": CAUSE_LABELS[cause],
                "amount_value_delta": new["amount_value"],
                "dispute_delta": new["dispute_amount"],
            })
            continue
        if new is None:
            line_deltas.append({
                "contract": contract, "box": box, "change": "计费行移除",
                "cause": CAUSE_OBSERVATION,
                "cause_label": CAUSE_LABELS[CAUSE_OBSERVATION],
                "amount_value_delta": -old["amount_value"],
                "dispute_delta": -old["dispute_amount"],
            })
            continue
        delta_value = round(new["amount_value"] - old["amount_value"], 2)
        delta_lo = round(new["amount_lo"] - old["amount_lo"], 2)
        delta_hi = round(new["amount_hi"] - old["amount_hi"], 2)
        delta_dispute = round(new["dispute_amount"] - old["dispute_amount"], 2)
        envelope_changed = (
            old["window_lo"] != new["window_lo"]
            or old["window_hi"] != new["window_hi"]
        )
        status_changed = new["status"] == "拦下" or old["status"] == "拦下"
        no_material_change = (
            not status_changed
            and delta_value == 0.0 and delta_lo == 0.0
            and delta_hi == 0.0 and delta_dispute == 0.0
        )
        if status_changed:
            cause = CAUSE_OBSERVATION
            note = "计价项目拦截/恢复，结转不计自动差额"
        elif box in evidence_boxes:
            cause = CAUSE_OBSERVATION
            note = "该箱存在新登记的更正或观察"
        elif box in linked_boxes and envelope_changed:
            cause = CAUSE_OBSERVATION
            note = "同船次新证据经共享名额/班组能力联动改变联合可行域"
        elif no_material_change:
            cause = CAUSE_ATTRIBUTION
            note = "无实质差额：政策口径与可行域均未变化，不进入成因合计"
        elif not envelope_changed and delta_value != 0.0:
            cause = CAUSE_ATTRIBUTION
            note = "证据区间与可行域未变，政策箱程/责任解释调整"
        else:
            cause = CAUSE_ATTRIBUTION
            note = "可行域变化但无新证据，按责任解释变化处理"
        line_deltas.append({
            "contract": contract, "box": box,
            "change": "无变化" if no_material_change else "数值更新",
            "cause": cause, "cause_label": CAUSE_LABELS[cause], "note": note,
            "material": not no_material_change,
            "window_lo_old": old["window_lo"], "window_lo_new": new["window_lo"],
            "window_hi_old": old["window_hi"], "window_hi_new": new["window_hi"],
            "amount_lo_delta": delta_lo,
            "amount_hi_delta": delta_hi,
            "amount_value_delta": delta_value,
            "dispute_delta": delta_dispute,
        })

    # 补救侧差额
    prev_rem = {(r["remedy"], r["box"]): r for r in previous.remedies["remedies"]}
    remedy_deltas: list[dict] = []
    for r in current.remedies["remedies"]:
        key = (r["remedy"], r["box"])
        old = prev_rem.get(key)
        if old is None:
            continue
        d_lo = r["verifiable_minutes_lo"] - old["verifiable_minutes_lo"]
        d_val = r["verifiable_minutes_value"] - old["verifiable_minutes_value"]
        if d_lo or d_val:
            remedy_deltas.append({
                "remedy": r["remedy"], "box": r["box"],
                "cause": CAUSE_REMEDY,
                "cause_label": CAUSE_LABELS[CAUSE_REMEDY],
                "minutes_lo_delta": d_lo,
                "minutes_value_delta": d_val,
                "note": "反事实可核实减时变化；证据驱动部分已在计费行差额中标注",
            })

    blocked_carry = [
        {"box": box, **info}
        for box, info in cur_delivery.items()
        if not info["feasible"]
    ]
    totals = {
        CAUSE_OBSERVATION: round(sum(
            d["amount_value_delta"] for d in line_deltas
            if d["cause"] == CAUSE_OBSERVATION
        ), 2),
        CAUSE_ATTRIBUTION: round(sum(
            d["amount_value_delta"] for d in line_deltas
            if d["cause"] == CAUSE_ATTRIBUTION and d.get("material", True)
        ), 2),
        CAUSE_REMEDY: round(sum(
            d["minutes_value_delta"] for d in remedy_deltas
        ), 2),
    }
    return {
        "previous_as_of": previous.manifest.get("as_of"),
        "current_as_of": current.manifest.get("as_of"),
        "new_evidence_boxes": sorted(evidence_boxes),
        "line_deltas": line_deltas,
        "remedy_deltas": remedy_deltas,
        "blocked_carry_forward": blocked_carry,
        "totals_by_cause": totals,
        "cause_labels": CAUSE_LABELS,
    }


def save_run(out_dir: Path | str, bundle: RunBundle) -> Path:
    out_dir = Path(out_dir)
    write_json(out_dir / "manifest.json", bundle.manifest)
    write_json(out_dir / "billing.json", bundle.billing)
    write_json(out_dir / "attribution.json", bundle.attribution)
    write_json(out_dir / "remedies.json", bundle.remedies)
    write_json(
        out_dir / "fleet.json",
        {
            vessel: [_plan_dict(p) for p in result["plans"]]
            for vessel, result in bundle.fleet.items()
        },
    )
    return out_dir


def _plan_dict(plan) -> dict:
    from dataclasses import asdict

    return {
        "box": plan.box, "vessel": plan.vessel, "day": plan.day,
        "feasible": plan.feasible, "blocked_reason": plan.blocked_reason,
        "milestones": {k: asdict(v) for k, v in plan.milestones.items()},
        "services": {k: asdict(v) for k, v in plan.services.items()},
    }


def load_run(run_dir: Path | str) -> RunBundle:
    import json

    run_dir = Path(run_dir)

    def read(name):
        return json.loads((run_dir / name).read_text(encoding="utf-8"))

    return RunBundle(
        manifest=read("manifest.json"),
        billing=read("billing.json"),
        attribution=read("attribution.json"),
        remedies=read("remedies.json"),
        fleet=read("fleet.json"),
    )
