"""交易池：按 (账户, nonce) 有序存放待打包交易，线程安全。"""
from __future__ import annotations

import threading

from .errors import RejectReason, TxRejected
from .models import Transaction


class Mempool:
    """待处理交易队列。

    规则：
    - 同 tx_id 只允许一笔；同 (sender, nonce) 也只允许一笔，
      重复或冲突的提交一律拒绝，绝不静默覆盖/顶掉池中已有交易。
    - 所有写操作都在同一把锁内完成，并发提交不会写乱队列。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._txs: dict[str, Transaction] = {}

    def add(self, tx: Transaction) -> None:
        with self._lock:
            if tx.tx_id in self._txs:
                raise TxRejected(RejectReason.DUPLICATE_TX, "同一笔交易已在池中")
            for pending in self._txs.values():
                if pending.sender == tx.sender and pending.nonce == tx.nonce:
                    raise TxRejected(
                        RejectReason.DUPLICATE_TX,
                        f"账户 {tx.sender} 的 nonce={tx.nonce} 已有待处理交易，拒绝覆盖",
                    )
            self._txs[tx.tx_id] = tx

    def contains(self, tx_id: str) -> bool:
        with self._lock:
            return tx_id in self._txs

    def pending_for(self, sender: str) -> list[Transaction]:
        """某账户在池中的交易，按 nonce 升序。"""
        with self._lock:
            txs = [t for t in self._txs.values() if t.sender == sender]
        return sorted(txs, key=lambda t: t.nonce)

    def pending_cost_for(self, sender: str) -> int:
        """某账户在池中所有交易的 amount+fee 总和（余额校验用）。"""
        with self._lock:
            return sum(
                t.amount + t.fee for t in self._txs.values() if t.sender == sender
            )

    def candidates(self) -> list[Transaction]:
        """全部候选，按 (手续费降序, tx_id 升序) 稳定排序。"""
        with self._lock:
            txs = list(self._txs.values())
        return sorted(txs, key=lambda t: (-t.fee, t.tx_id))

    def remove_many(self, tx_ids: list[str]) -> None:
        with self._lock:
            for tx_id in tx_ids:
                self._txs.pop(tx_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._txs)
