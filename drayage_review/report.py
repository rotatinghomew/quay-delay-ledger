"""中文终端报告渲染。"""
from __future__ import annotations


def _fmt_minutes(v: int) -> str:
    return f"{v} 分钟"


def render_manifest(manifest: dict) -> str:
    lines = [
        f"联合复盘运行 {manifest['run_id']}",
        f"账期 as-of：{manifest['as_of'] or '全部可见资料'}",
        f"箱程数：{manifest['boxes']}，可行：{manifest['feasible_boxes']}，"
        f"拦下：{manifest['blocked_boxes']}",
        f"被修订归并/尚未可见记录：{manifest['suppressed_records']} 条",
    ]
    if manifest.get("blocked_detail"):
        lines.append("拦下箱程：")
        for item in manifest["blocked_detail"]:
            lines.append(f"  - {item['box']}（{item['vessel']}）：{item['reason']}")
    return "\n".join(lines)


def render_box(fleet: dict, box: str, attribution: dict) -> str:
    plan = None
    vessel_name = None
    for vessel, result in fleet.items():
        if isinstance(result, list):
            plans = result
        elif isinstance(result, dict):
            plans = result["plans"] if "plans" in result else result
        else:
            plans = result.plans
        for candidate in plans:
            cbox = candidate["box"] if isinstance(candidate, dict) else candidate.box
            if cbox == box:
                plan = candidate
                vessel_name = vessel
    if plan is None:
        return f"未找到箱程 {box}"
    if isinstance(plan, dict):
        if not plan["feasible"]:
            return f"{box}：无可行箱程，已拦下（{plan['blocked_reason']}）"
        ms = plan["milestones"]
        sv = plan["services"]
        lines = [f"箱程 {box}（船次 {vessel_name}，{plan['day']}）联合可行包络："]
        for kind, m in ms.items():
            lines.append(
                f"  {kind:<6} 证据 {m['lo']}-{m['hi']} | 可行域 {m['min']}-{m['max']}"
                f" | 政策 {m['value']}（rev{m['revision']}，车辆 {m.get('vehicle') or '—'}）"
            )
        for key, s in sv.items():
            lines.append(
                f"  {s['kind']:<6} 设施 {s['facility']} 窗口 {s['open']}-{s['close']}"
                f" 占 {s['service']} | 开始可行域 {s['start_min']}-{s['start_max']}"
                f" | 政策 {s['start']}-{s['end']}"
            )
    else:
        if not plan.feasible:
            return f"{box}：无可行箱程，已拦下（{plan.blocked_reason}）"
        lines = [f"箱程 {box}（船次 {vessel_name}，{plan.day}）联合可行包络："]
        for kind, m in plan.milestones.items():
            lines.append(
                f"  {kind:<6} 证据 {m.lo}-{m.hi} | 可行域 {m.min}-{m.max}"
                f" | 政策 {m.value}（rev{m.revision}，车辆 {m.vehicle or '—'}）"
            )
        for key, s in plan.services.items():
            lines.append(
                f"  {s.kind:<6} 设施 {s.facility} 窗口 {s.open}-{s.close}"
                f" 占 {s.service} | 开始可行域 {s.start_min}-{s.start_max}"
                f" | 政策 {s.start}-{s.end}"
            )

    lines.append("边际反事实归因（减时分钟，不做各方本地节省相加）：")
    for vessel_attrs in attribution.values():
        for ba in vessel_attrs:
            ba_box = ba["box"] if isinstance(ba, dict) else ba.box
            if ba_box != box:
                continue
            facilities = ba["facilities"]
            for fa in facilities:
                lines.append(
                    f"  {fa['facility']}（{fa['party'] or '无合同方'}）："
                    f"窗口可归因 {fa['window_relief']}，名额可归因 {fa['capacity_relief']}，"
                    f"仅联合范围 {fa['joint_only_relief']}，反事实上界 {fa['max_relief']}"
                )
    return "\n".join(lines)


def render_vessel_remedy(remedy_report: dict, vessel_boxes: dict,
                         vessel: str) -> str:
    boxes = set(vessel_boxes.get(vessel, []))
    lines = [f"船次 {vessel} 补救情景对比（交付时刻，分钟）："]
    header = "  情景".ljust(18) + "".join(b.rjust(9) for b in sorted(boxes)) + "本船合计".rjust(10)
    lines.append(header)
    for sc in remedy_report["scenarios"]:
        row = "  " + sc["label"].ljust(16)
        vessel_total = 0
        for b in sorted(boxes):
            if b in sc["blocked"]:
                row += "拦下".rjust(9)
            else:
                value = sc["deliveries"].get(b)
                row += ("—" if value is None else str(value)).rjust(9)
                if value is not None:
                    vessel_total += value
        row += str(vessel_total).rjust(10)
        lines.append(row)
    lines.append("")
    lines.append("补救评估：")
    for ev in remedy_report["remedies"]:
        if ev["box"] not in boxes:
            continue
        lines.append(
            f"  {ev['remedy']} 箱 {ev['box']} 设施 {ev['facility']}："
            f"可核实减时 {ev['verifiable_minutes_lo']}-{ev['verifiable_minutes_hi']}"
            f"（政策 {ev['verifiable_minutes_value']}）分钟，"
            f"可分配 {'是' if ev['allocable'] else '否'}，"
            f"补助资格 {'有' if ev['eligible_for_subsidy'] else '无'}"
        )
        lines.append(f"    分配说明：{ev['allocation_note']}")
        lines.append(f"    补助说明：{ev['eligibility_note']}")
    return "\n".join(lines)


def render_contract(billing: dict, contract: str) -> str:
    term = next((t for t in billing["terms"] if t["contract"] == contract), None)
    if term is None:
        return f"未找到合同批次 {contract}"
    lines = [
        f"合同 {contract}（{term['party']}，设施 {term['facility']}，"
        f"费率 {term['rate']} 元/分钟，保留额度 {term['reserved']} 元）",
        f"窗口 {term['lines'][0]['count_from'] if term['lines'] else '—'} -> "
        f"{term['lines'][0]['count_to'] if term['lines'] else '—'}，"
        f"免费等待见逐行",
        "  箱号".ljust(8) + "窗口区间".rjust(14) + "可计区间".rjust(14)
        + "计费区间(元)".rjust(20) + "争议额".rjust(10) + "状态".rjust(8),
    ]
    for line in term["lines"]:
        lines.append(
            "  " + line["box"].ljust(6)
            + f"{line['window_lo']}-{line['window_hi']}".rjust(14)
            + f"{line['billable_lo']}-{line['billable_hi']}".rjust(14)
            + f"{line['amount_lo']}-{line['amount_hi']}".rjust(20)
            + f"{line['dispute_amount']}".rjust(10)
            + line["status"].rjust(8)
        )
        if line["status"] == "拦下":
            lines.append(f"      拦下原因：{line['blocked_reason']}")
    lines.append(
        f"合计：可计费 {term['amount_lo']}-{term['amount_hi']} 元，"
        f"政策口径 {term['amount_value']} 元，争议差额 {term['dispute_amount']} 元"
    )
    return "\n".join(lines)


def render_remedy_report(remedy_report: dict) -> str:
    lines = ["全部补救情景（合计交付时刻，分钟）："]
    for sc in remedy_report["scenarios"]:
        lines.append(f"  {sc['label']:<18} 合计 {sc['total_delivery']}")
    lines.append("")
    lines.append("确认单核对：")
    for acc in remedy_report["acceptances"]:
        head = (
            f"  {acc['acceptance']} 合同 {acc['contract']} 箱 {acc['box']} "
            f"补救 {acc['remedy']} 额度 {acc['amount']} 元 状态 {acc['state']}"
        )
        lines.append(head)
        for flag in acc["flags"]:
            lines.append(f"    ! {flag}")
    return "\n".join(lines)


def render_reconciliation(diff: dict) -> str:
    lines = [
        f"联合对账：{diff['previous_as_of']} -> {diff['current_as_of']}",
        f"新证据涉及箱：{', '.join(diff['new_evidence_boxes']) or '无'}",
        "逐合同行差额（无实质变化的行已折叠）：",
    ]
    shown = 0
    for d in diff["line_deltas"]:
        if d.get("material") is False:
            continue
        shown += 1
        lines.append(
            f"  {d['contract']} {d['box']} [{d['cause_label']}] {d['change']}："
            f"政策口径差额 {d.get('amount_value_delta', 0)} 元，"
            f"争议额变化 {d.get('dispute_delta', 0)} 元"
            + (f"（{d['note']}）" if d.get("note") else "")
        )
    if shown == 0:
        lines.append("  （无）")
    if diff["remedy_deltas"]:
        lines.append("补救可核实减时变化：")
        for d in diff["remedy_deltas"]:
            lines.append(
                f"  {d['remedy']} {d['box']}：政策口径 {d['minutes_value_delta']} 分钟"
            )
    lines.append("成因合计：" + "，".join(
        f"{k}={v}" for k, v in diff["totals_by_cause"].items()
    ))
    if diff["blocked_carry_forward"]:
        lines.append("拦下结转：" + "，".join(
            f"{b['box']}（{b['blocked_reason']}）"
            for b in diff["blocked_carry_forward"]
        ))
    return "\n".join(lines)
