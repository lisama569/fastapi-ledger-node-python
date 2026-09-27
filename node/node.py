"""节点门面：一把锁保护全部可变状态，供 API 与测试使用。"""
from __future__ import annotations

import dataclasses
import threading

from .chain import SKIP, STRICT, Blockchain
from .errors import LedgerError, RejectReason
from .mempool import Mempool
from .models import Block, Transaction, block_from_dict, build_block
from .state import WorldState


@dataclasses.dataclass(slots=True)
class ProduceOutcome:
    block_id: str
    included_tx_ids: tuple[str, ...]
    skipped_tx_ids: tuple[str, ...]


@dataclasses.dataclass(slots=True)
class ReceiveOutcome:
    block_id: str
    accepted: bool
    switched: bool
    tip_id: str
    policy: str
    included_tx_ids: tuple[str, ...] = ()
    excluded: tuple[dict, ...] = ()
    reject_reason: str | None = None
    detail: str = ""


class LedgerNode:
    """本地简化链节点。所有写操作在同一把可重入锁内完成。"""

    def __init__(self, allocations: dict[str, int] | None = None) -> None:
        self._lock = threading.RLock()
        self.allocations = dict(allocations or {})
        self.chain = Blockchain.create(self.allocations)
        self.mempool = Mempool(state=self.chain.tip_state)
        self.last_switch_dropped: list[dict] = []

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _tx_dict(tx: Transaction) -> dict:
        d = tx.to_canonical_dict()
        d["cost"] = tx.cost()
        return d

    def _sync_tip(self) -> bool:
        """把池子基准状态切到当前链头，并重校所有待处理交易。"""
        switched = self.mempool.state is not self.chain.tip_state
        if switched:
            dropped = self.mempool.rebuild_after_switch(self.chain.tip_state)
            self.last_switch_dropped = [
                {"tx_id": tx_id, "reason": str(reason), "detail": detail}
                for tx_id, reason, detail in dropped
            ]
        return switched

    # ---------------------------------------------------------------- queries
    def account(self, name: str) -> dict:
        with self._lock:
            acc = self.chain.tip_state.accounts.get(name)
            queued = [
                self._tx_dict(tx)
                for (sender, _n), tx in sorted(self.mempool.pending.items())
                if sender == name
            ]
            if acc is None:
                return {
                    "name": name,
                    "exists": False,
                    "balance": 0,
                    "nonce": 0,
                    "queued": queued,
                }
            return {
                "name": name,
                "exists": True,
                "balance": acc.balance,
                "nonce": acc.nonce,
                "queued": queued,
            }

    def total_supply(self) -> int:
        with self._lock:
            return self.chain.tip_state.total_supply()

    def tip(self) -> dict:
        with self._lock:
            blk = self.chain.tip
            return {
                "block_id": blk.block_id,
                "height": blk.height,
                "parent_id": blk.parent_id,
                "producer": blk.producer,
                "weight": blk.weight,
                "cumulative_weight": self.chain.chain_weight(blk.block_id),
                "tx_count": len(blk.transactions),
            }

    def pending(self) -> list[dict]:
        with self._lock:
            return [
                self._tx_dict(tx) for tx in self.mempool.ordered_candidates()
            ]

    def block(self, block_id: str) -> dict:
        with self._lock:
            blk = self.chain.get_block(block_id)
            if blk is None:
                raise LedgerError(
                    RejectReason.BAD_INPUT, f"区块不存在: {block_id}"
                )
            result = self.chain.results.get(block_id)
            return {
                "block_id": blk.block_id,
                "height": blk.height,
                "parent_id": blk.parent_id,
                "producer": blk.producer,
                "weight": blk.weight,
                "cumulative_weight": self.chain.chain_weight(block_id),
                "transactions": [t.to_canonical_dict() for t in blk.transactions],
                "result": None
                if result is None
                else {
                    "accepted": result.accepted,
                    "policy": result.policy,
                    "included": [r.to_dict() for r in result.included],
                    "excluded": [r.to_dict() for r in result.excluded],
                    "detail": result.detail,
                },
            }

    def state_check(self) -> dict:
        with self._lock:
            tip_id = self.chain.tip_id
            replayed = self.chain.replay_state(tip_id)
            cached = self.chain.tip_state
            return {
                "tip_id": tip_id,
                "total_supply": cached.total_supply(),
                "genesis_supply": self.chain.state_at("GENESIS").total_supply(),
                "supply_conserved": cached.total_supply()
                == self.chain.state_at("GENESIS").total_supply(),
                "replay_matches_snapshot": replayed.accounts == cached.accounts,
                "known_blocks": len(self.chain.blocks),
                "active_chain": self.chain.active_ids(),
                "pending_count": len(self.mempool.pending),
            }

    # ------------------------------------------------------------------ writes
    def submit_tx(self, data: dict) -> dict:
        with self._lock:
            tx = Transaction.from_dict(data)
            self.mempool.submit(tx)
            return {
                "status": "accepted",
                "tx": self._tx_dict(tx),
                "position_rule": "按 (fee 降序, tx_id 升序) 参与出块排队",
            }

    def produce_block(self, producer: str, weight: int = 1) -> dict:
        with self._lock:
            if not isinstance(producer, str) or not producer:
                raise LedgerError(
                    RejectReason.BAD_INPUT, "producer 必须是非空字符串"
                )
            if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
                raise LedgerError(
                    RejectReason.BAD_INPUT, "weight 必须是正整数"
                )
            parent = self.chain.tip
            chosen = self.mempool.select_for_block(producer)
            skipped = [
                tx.tx_id
                for tx in self.mempool.ordered_candidates()
                if tx not in chosen
            ]
            block = build_block(
                height=parent.height + 1,
                parent_id=parent.block_id,
                producer=producer,
                transactions=chosen,
                weight=weight,
            )
            # 自选块一定合法，但仍统一走 receive（strict），保证只有一条执行路径
            result = self.chain.receive_block(block, STRICT)
            if not result.accepted:
                # 理论上不可达：自选交易全部通过了试应用
                raise LedgerError(
                    result.reject_reason or RejectReason.BAD_BLOCK_STRUCTURE,
                    f"自选块执行失败（不应发生）: {result.detail}",
                )
            self.mempool.remove_included(chosen)
            self._sync_tip()
            return {
                "status": "produced",
                "block_id": block.block_id,
                "height": block.height,
                "included_tx_ids": [t.tx_id for t in chosen],
                "skipped_tx_ids": skipped,
                "switched": self.chain.tip_id == block.block_id,
            }

    def receive_block(self, data: dict, policy: str = STRICT) -> dict:
        with self._lock:
            if policy not in (STRICT, SKIP):
                raise LedgerError(RejectReason.BAD_INPUT, f"未知块处理策略: {policy}")
            block = block_from_dict(data)
            result = self.chain.receive_block(block, policy)
            switched = self._sync_tip()
            if result.accepted:
                included_ids = {r.tx_id for r in result.included}
                included_txs = [
                    tx for tx in block.transactions if tx.tx_id in included_ids
                ]
                self.mempool.remove_included(included_txs)
                return {
                    "status": "accepted",
                    "block_id": block.block_id,
                    "policy": policy,
                    "included_tx_ids": [tx.tx_id for tx in included_txs],
                    "excluded": [r.to_dict() for r in result.excluded],
                    "switched": switched,
                    "tip_id": self.chain.tip_id,
                    "detail": result.detail,
                    "dropped_from_mempool": self.last_switch_dropped if switched else [],
                }
            return {
                "status": "rejected"
                if result.reject_reason != RejectReason.DUPLICATE_BLOCK
                else "duplicate",
                "block_id": block.block_id,
                "policy": policy,
                "switched": switched,
                "tip_id": self.chain.tip_id,
                "reject_reason": None
                if result.reject_reason is None
                else str(result.reject_reason),
                "excluded": [r.to_dict() for r in result.excluded],
                "detail": result.detail,
            }
