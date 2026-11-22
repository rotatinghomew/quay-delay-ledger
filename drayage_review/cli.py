"""命令行入口：把联合复盘流程组装成可反复处理账期的业务工具。

子命令：

* ``run``          生成一次联合复盘（调度+归因+计费+补救）并落盘
* ``box``          按箱查共享等待与归因
* ``vessel``       按船次比较补救情景
* ``contract``     按合同批次追可计费时间、争议差额与书面确认额度
* ``reconcile``    与上一账期运行对比，分解差额成因
"""
from __future__ import annotations

import argparse
import uuid
from datetime import date
from pathlib import Path

from . import report
from .attribution import attribute_fleet
from .billing import build_billing
from .io import Dataset, write_json
from .periods import RunBundle, compare_runs, load_run, save_run
from .remedies import evaluate_scenarios
from .scheduler import solve_fleet


def _default_as_of(ds: Dataset) -> str:
    candidates = [row["available"] for row in ds.events]
    return max(candidates) if candidates else date.today().isoformat()


def run_review(data_dir: str, as_of: str | None, out_dir: str,
               do_remedies: bool = True) -> RunBundle:
    ds = Dataset.load(data_dir, as_of)
    if as_of is None:
        as_of = _default_as_of(Dataset.load(data_dir, None))
    fleet = solve_fleet(ds, with_ranges=True)
    attribution = attribute_fleet(ds, fleet)
    billing = build_billing(ds, fleet)
    remedy_report = evaluate_scenarios(ds) if do_remedies else {
        "scenarios": [], "remedies": [], "acceptances": []
    }

    blocked_detail = [
        {"box": box, "vessel": vessel, "reason": reason}
        for vessel, result in fleet.items()
        for box, reason in result["blocked"]
    ]
    manifest = {
        "run_id": uuid.uuid4().hex[:12],
        "as_of": as_of,
        "generated_on": date.today().isoformat(),
        "data_dir": str(Path(data_dir).resolve()),
        "boxes": len(ds.cargo),
        "feasible_boxes": sum(
            1 for r in fleet.values() for p in r["plans"] if p.feasible
        ),
        "blocked_boxes": len(blocked_detail),
        "blocked_detail": blocked_detail,
        "suppressed_records": len(ds.suppressed_revisions),
        "suppressed_detail": [
            {"event": r["event"], "box": r["box"], "revision": r["revision"],
             "available": r["available"]}
            for r in ds.suppressed_revisions
        ],
    }
    attr_payload = {
        vessel: [ba.to_dict() for ba in items]
        for vessel, items in attribution.items()
    }
    bundle = RunBundle(
        manifest=manifest, billing=billing,
        attribution=attr_payload, remedies=remedy_report, fleet=fleet,
    )
    save_run(out_dir, bundle)
    write_json(Path(out_dir) / "dataset_snapshot.json", {
        "as_of": as_of,
        "visible_events": ds.events,
        "suppressed_revisions": ds.suppressed_revisions,
    })
    return bundle


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="drayage-review",
        description="港区转运减时联合复盘与合同额度工具",
    )
    p.add_argument("--data-dir", default="data", help="业务资料目录")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="生成一次联合复盘")
    pr.add_argument("--as-of", default=None, help="账期日 YYYY-MM-DD")
    pr.add_argument("--out", required=True, help="运行产物目录")
    pr.add_argument("--no-remedies", action="store_true")

    pb = sub.add_parser("box", help="按箱查共享等待与归因")
    pb.add_argument("--box", required=True)
    pb.add_argument("--run", required=True, help="已落盘的运行目录")

    pv = sub.add_parser("vessel", help="按船次比较补救效果")
    pv.add_argument("--vessel", required=True)
    pv.add_argument("--run", required=True)

    pc = sub.add_parser("contract", help="按合同批次追计费与确认额度")
    pc.add_argument("--contract", required=True)
    pc.add_argument("--run", required=True)

    pj = sub.add_parser("reconcile", help="与上一账期运行对比")
    pj.add_argument("--previous", required=True)
    pj.add_argument("--current", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "run":
        bundle = run_review(
            args.data_dir, args.as_of, args.out,
            do_remedies=not args.no_remedies,
        )
        print(report.render_manifest(bundle.manifest))
        print()
        print(report.render_remedy_report(bundle.remedies))
        return 0

    if args.command == "reconcile":
        previous = load_run(args.previous)
        current = load_run(args.current)
        ds = Dataset.load(args.data_dir, current.manifest["as_of"])
        diff = compare_runs(previous, current, ds)
        write_json(Path(args.current) / "reconciliation.json", diff)
        print(report.render_reconciliation(diff))
        return 0

    bundle = load_run(args.run)
    if args.command == "box":
        print(report.render_box(bundle.fleet, args.box, bundle.attribution))
    elif args.command == "vessel":
        ds = Dataset.load(args.data_dir)
        print(report.render_vessel_remedy(
            bundle.remedies, ds.boxes_by_vessel(), args.vessel
        ))
    elif args.command == "contract":
        print(report.render_contract(bundle.billing, args.contract))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
