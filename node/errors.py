"""错误分类：所有拒绝原因都集中在这里，便于区分与测试。"""
from __future__ import annotations

import enum


class RejectReason(enum.Enum):
    """交易/区块被拒绝的可区分原因。"""

    # 交易级
    DUPLICATE_TX = "DUPLICATE_TX"              # 重复提交：池中已有同笔交易或同 账户+nonce 的交易
    NONCE_TOO_LOW = "NONCE_TOO_LOW"            # 序号低于账户当前 nonce（已上链）
    NONCE_GAP = "NONCE_GAP"                    # 序号断层：高于下一个期望 nonce
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"  # 余额不足以支付 amount + fee
    INVALID_TRANSACTION = "INVALID_TRANSACTION"  # 金额/手续费为负、字段非法等

    # 区块级
    UNKNOWN_PARENT = "UNKNOWN_PARENT"          # 父块不存在（孤儿块）
    DUPLICATE_BLOCK = "DUPLICATE_BLOCK"        # 同一区块重复送达
    INVALID_BLOCK = "INVALID_BLOCK"            # 块内交易非法（整块拒绝策略下）
    INVALID_HEIGHT = "INVALID_HEIGHT"          # 高度与父块不连续


class TxRejected(Exception):
    """交易被拒绝，携带可区分的原因。"""

    def __init__(self, reason: RejectReason, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")


class BlockRejected(Exception):
    """区块被拒绝，携带可区分的原因。"""

    def __init__(self, reason: RejectReason, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")
