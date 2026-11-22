"""资料读取、修订归并、as-of 证据过滤与运行产物落盘。

业务约定：

* 同一 ``(event, box, kind, day)`` 存在多个 ``revision`` 时，柜台更正覆盖原记录；
  高版本记录在 ``available`` 日之后才对复盘可见。
* 任何观察记录都按 ``available`` 参与账期可见性过滤，使同一工具可反复处理账期：
  早期账期看不到后来才登记的更正与观察。
* 箱程身份是 ``box``（按船次组织），换车辆不改变箱程；同一预约号可含多只箱，
  因此车辆、预约只作展示与分组，不作为身份键。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

TABLES = (
    "cargo",
    "events",
    "barriers",
    "capacity",
    "terms",
    "remedies",
    "acceptances",
)


def _iso(d: str) -> date:
    return date.fromisoformat(d)


def load_table(data_dir: Path | str, name: str) -> list[dict[str, Any]]:
    path = Path(data_dir) / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_all(data_dir: Path | str) -> dict[str, list[dict[str, Any]]]:
    return {name: load_table(data_dir, name) for name in TABLES}


def visible_events(
    events: list[dict[str, Any]], as_of: str | None
) -> list[dict[str, Any]]:
    """按账期日过滤可见事件，并把同一事件的多个修订归并为当前有效版本。

    只有 ``available <= as_of`` 的记录可见；同一身份键取最高 ``revision``。
    """
    cutoff = _iso(as_of) if as_of else None
    current: dict[tuple, dict[str, Any]] = {}
    for row in events:
        if cutoff is not None and _iso(row["available"]) > cutoff:
            continue
        key = (row["event"], row["box"], row["kind"], row["day"])
        old = current.get(key)
        if old is None or row["revision"] > old["revision"]:
            current[key] = row
    return list(current.values())


@dataclass
class Dataset:
    """一次复盘可见的全部资料（已做修订归并与账期过滤）。"""

    as_of: str | None
    cargo: list[dict[str, Any]]
    events: list[dict[str, Any]]
    barriers: list[dict[str, Any]]
    capacity: list[dict[str, Any]]
    terms: list[dict[str, Any]]
    remedies: list[dict[str, Any]]
    acceptances: list[dict[str, Any]]
    suppressed_revisions: list[dict[str, Any]] = field(default_factory=list)
    ordered_handoff_boxes: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, data_dir: Path | str, as_of: str | None = None) -> "Dataset":
        raw = load_all(data_dir)
        cutoff = _iso(as_of) if as_of else None
        suppressed = [
            row
            for row in raw["events"]
            if cutoff is not None and _iso(row["available"]) > cutoff
        ]
        events = visible_events(raw["events"], as_of)
        ordered_path = Path(data_dir) / "ordered_handoff.json"
        ordered = set()
        if ordered_path.exists():
            payload = json.loads(ordered_path.read_text(encoding="utf-8"))
            ordered = set(payload.get("ordered_handoff_boxes", []))
        ds = cls(
            as_of=as_of,
            cargo=raw["cargo"],
            events=events,
            barriers=raw["barriers"],
            capacity=raw["capacity"],
            terms=raw["terms"],
            remedies=raw["remedies"],
            acceptances=raw["acceptances"],
            suppressed_revisions=suppressed,
        )
        ds.ordered_handoff_boxes = ordered
        return ds

    # ---- 索引 ----------------------------------------------------------------
    def cargo_by_box(self) -> dict[str, dict[str, Any]]:
        return {row["box"]: row for row in self.cargo}

    def boxes_by_vessel(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for row in sorted(self.cargo, key=lambda r: r["box"]):
            grouped.setdefault(row["vessel"], []).append(row["box"])
        return grouped

    def milestones(self, box: str) -> dict[str, dict[str, Any]]:
        """该箱当前有效的交接节点 -> 事件记录（lo/hi 区间）。"""
        out: dict[str, dict[str, Any]] = {}
        for row in self.events:
            if row["box"] == box:
                out[row["kind"]] = row
        return out

    def box_barriers(self, box: str) -> dict[str, dict[str, Any]]:
        """该箱三段服务限制，键为 berth/yard/dray。"""
        from . import SERVICE_BY_BARRIER_KIND

        out: dict[str, dict[str, Any]] = {}
        for row in self.barriers:
            if row["box"] == box:
                key = SERVICE_BY_BARRIER_KIND[row["kind"]]
                out[key] = row
        return out

    def vessel_capacity(self, vessel: str, day: str) -> dict[str, dict[str, Any]]:
        """船次运行日的共享能力，键为设施。"""
        return {
            row["facility"]: row
            for row in self.capacity
            if row["vessel"] == vessel and row["day"] == day
        }

    def term(self, contract: str) -> dict[str, Any]:
        for row in self.terms:
            if row["contract"] == contract:
                return row
        raise KeyError(contract)


def write_json(path: Path | str, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
