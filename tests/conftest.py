"""共享测试工具：打印输入与判定依据，方便跑单测时看清每一步。"""
from __future__ import annotations

from node.models import Transaction


def show_tx(prefix: str, tx: Transaction) -> None:
    print(
        f"{prefix}: sender={tx.sender} recipient={tx.recipient} nonce={tx.nonce} "
        f"amount={tx.amount} fee={tx.fee} id={tx.tx_id[:8]}"
    )
