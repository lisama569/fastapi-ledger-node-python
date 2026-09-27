"""数据模型：交易、区块、账户状态。无真实密码学，id 用 sha256 摘要代替签名。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


def _digest(payload: dict) -> str:
    """对字段做确定性序列化后取 sha256，作为本地 id。"""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class Transaction:
    """一笔转账：sender 付 amount 给 recipient，另付 fee 给出块者。"""

    sender: str
    recipient: str
    nonce: int
    amount: int
    fee: int

    @property
    def tx_id(self) -> str:
        return _digest(
            {
                "sender": self.sender,
                "recipient": self.recipient,
                "nonce": self.nonce,
                "amount": self.amount,
                "fee": self.fee,
            }
        )

    def basic_valid(self) -> bool:
        """字段级合法性：金额为正、手续费非负、nonce 非负、账户名非空。"""
        return (
            bool(self.sender)
            and bool(self.recipient)
            and self.nonce >= 0
            and self.amount > 0
            and self.fee >= 0
        )


@dataclass(frozen=True)
class Block:
    """一个区块：挂在 parent_id 之后，携带一组有序交易。"""

    height: int
    parent_id: str
    producer: str
    txs: tuple[Transaction, ...] = field(default_factory=tuple)

    @property
    def block_id(self) -> str:
        return _digest(
            {
                "height": self.height,
                "parent_id": self.parent_id,
                "producer": self.producer,
                "txs": [t.tx_id for t in self.txs],
            }
        )


@dataclass
class AccountState:
    """账户账本状态：余额与已用 nonce（下一笔期望 nonce = nonce）。"""

    balance: int = 0
    nonce: int = 0
