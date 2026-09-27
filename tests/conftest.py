"""测试公共工具：初始化节点与"判定依据打印"。"""
from __future__ import annotations

import pprint


def show(title: str, payload) -> None:
    """把测试输入与判定依据打印到 stdout（pytest -s 或 -rA 时可见）。"""
    print(f"\n===== {title} =====")
    pprint.pp(payload, sort_dicts=False)


def explain(title: str, evidence) -> None:
    print(f"----- 判定依据 [{title}] -----")
    pprint.pp(evidence, sort_dicts=False)
