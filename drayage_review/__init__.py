"""港区转运减时联合复盘工具。

模块划分：

* ``io``         资料读取、as-of 证据过滤与修订归并、运行产物落盘
* ``scheduler``  可行箱程混合整数调度引擎（含冲突隔离与可行域极值）
* ``attribution`` 边际反事实归因（明确可归因 vs 仅联合范围）
* ``billing``    合同可计费窗口、免费时间与争议差额
* ``remedies``   补救反事实情景、组合收益、补助资格与重复认领防护
* ``periods``    账期滚动与差额成因分解
* ``report``     中文文本与 JSON 报告渲染
* ``cli``        命令行入口
"""

__version__ = "1.0.0"

# 交接节点（里程碑）标准顺序
MILESTONES = ("船侧交接", "放行", "短驳接手", "交付")

# 箱侧 barriers 的 kind 到内部服务键的映射
SERVICE_BY_BARRIER_KIND = {
    "开放泊位": "berth",
    "堆场复核": "yard",
    "短驳班组": "dray",
}

# 服务键到设施角色的说明
SERVICE_LABEL = {
    "berth": "船口开放泊位",
    "yard": "堆场复核位",
    "dray": "短驳班组",
}

# 设施 -> 对应合同方（terms 中按 facility 对齐；专线无合同）
FACILITY_PARTY = {
    "船口": "码头",
    "公用": "堆场",
    "全港": "末端接驳",
}
