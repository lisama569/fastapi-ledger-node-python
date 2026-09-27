"""链存储与分叉选择：按块保存状态快照，支持链头切换与状态回放。"""
from __future__ import annotations

import enum
import threading
from dataclasses import dataclass, field

from .errors import BlockRejected, RejectReason
from .models import Block, Transaction
from .state import LedgerState


class BlockApplyPolicy(enum.Enum):
    """块内混入非法交易时的处理策略（必须在结果里说得清是哪一种）。"""

    REJECT_WHOLE_BLOCK = "REJECT_WHOLE_BLOCK"  # 整块拒绝，一笔都不应用
    SKIP_INVALID_TX = "SKIP_INVALID_TX"        # 剔除非法单笔，应用其余合法交易


@dataclass
class AddBlockResult:
    """收块结果：是否切换了链头，以及被剔除的交易（SKIP 策略下）。"""

    switched_head: bool
    skipped: list[tuple[str, str]] = field(default_factory=list)
    block_id: str = ""


def genesis_block() -> Block:
    return Block(height=0, parent_id="", producer="genesis", txs=())


class Chain:
    """本地区块链。

    选链（链头切换）规则：
    - 权重 = 区块高度 + 1（链越长权重越大）；
    - 新块权重严格大于当前链头才切换；相等不切换（先到先得）。
    每个块入链时保存其应用后的完整状态快照；任何已入链的块
    （包括后来被切走的分支）都能通过 state_at 找回状态。
    """

    def __init__(self, genesis_alloc: dict[str, int]) -> None:
        self._lock = threading.RLock()
        self._blocks: dict[str, Block] = {}
        self._snapshots: dict[str, LedgerState] = {}
        self._weights: dict[str, int] = {}
        genesis = genesis_block()
        self._genesis_id = genesis.block_id
        self._blocks[self._genesis_id] = genesis
        self._snapshots[self._genesis_id] = LedgerState(genesis_alloc)
        self._weights[self._genesis_id] = 1
        self._head_id = self._genesis_id

    @property
    def head_id(self) -> str:
        return self._head_id

    @property
    def head(self) -> Block:
        return self._blocks[self._head_id]

    def genesis_id(self) -> str:
        return self._genesis_id

    def has_block(self, block_id: str) -> bool:
        with self._lock:
            return block_id in self._blocks

    def head_state(self) -> LedgerState:
        return self.state_at(self._head_id)

    def state_at(self, block_id: str) -> LedgerState:
        """任意已入链区块之后的状态快照（返回拷贝，含被切走的分支）。"""
        with self._lock:
            snap = self._snapshots[block_id]
        return snap.snapshot()

    def add_block(
        self, block: Block, policy: BlockApplyPolicy = BlockApplyPolicy.REJECT_WHOLE_BLOCK
    ) -> AddBlockResult:
        """接入区块并按需切换链头。

        - 未知父块：拒绝（孤儿块）。
        - 已存在的 block_id：拒绝，不会重复计入状态。
        - 块内交易合法性按 policy 处理：整块拒绝 或 单笔剔除（结果中说明）。
        """
        with self._lock:
            block_id = block.block_id
            if block_id in self._blocks:
                raise BlockRejected(
                    RejectReason.DUPLICATE_BLOCK, f"区块已存在: {block_id[:12]}"
                )
            parent = self._blocks.get(block.parent_id)
            if parent is None:
                raise BlockRejected(
                    RejectReason.UNKNOWN_PARENT, f"未知父块: {block.parent_id[:12]}"
                )
            if block.height != parent.height + 1:
                raise BlockRejected(
                    RejectReason.INVALID_HEIGHT,
                    f"高度不连续: 块高 {block.height}, 父块高 {parent.height}",
                )

            # 在父块快照的拷贝上应用；无论成功与否都不会污染已保存的状态。
            working = self.state_at(block.parent_id)
            skipped: list[tuple[str, str]] = []
            for index, tx in enumerate(block.txs):
                reason = self._invalid_reason(working, tx)
                if reason is not None:
                    if policy is BlockApplyPolicy.REJECT_WHOLE_BLOCK:
                        # 整块拒绝：working 直接丢弃，不会扣一半/算两次。
                        raise BlockRejected(
                            RejectReason.INVALID_BLOCK,
                            f"第 {index} 笔交易非法: {reason.value} ({tx.tx_id[:12]})",
                        )
                    skipped.append((tx.tx_id, reason.value))
                    continue
                working.apply_tx(tx, block.producer)

            supply = working.total_supply()
            genesis_supply = self._snapshots[self._genesis_id].total_supply()
            assert supply == genesis_supply, (
                f"出块后总量对不上: {supply} != {genesis_supply}"
            )

            self._blocks[block_id] = block
            self._snapshots[block_id] = working  # 之后只通过拷贝读取
            weight = block.height + 1
            self._weights[block_id] = weight

            switched = weight > self._weights[self._head_id]
            if switched:
                self._head_id = block_id
            return AddBlockResult(
                switched_head=switched, skipped=skipped, block_id=block_id
            )

    @staticmethod
    def _invalid_reason(state: LedgerState, tx: Transaction) -> RejectReason | None:
        """返回交易在该状态下的非法原因；合法返回 None。"""
        if not tx.basic_valid():
            return RejectReason.INVALID_TRANSACTION
        st = state.get(tx.sender)
        if st.nonce != tx.nonce:
            if st.nonce > tx.nonce:
                return RejectReason.NONCE_TOO_LOW
            return RejectReason.NONCE_GAP
        if st.balance < tx.amount + tx.fee:
            return RejectReason.INSUFFICIENT_FUNDS
        return None

    def path_from_genesis(self, block_id: str) -> list[Block]:
        """从创世到指定区块的有序路径（用于回放校验）。"""
        with self._lock:
            if block_id not in self._blocks:
                raise KeyError(block_id)
            path: list[Block] = []
            cur = block_id
            while True:
                block = self._blocks[cur]
                path.append(block)
                if cur == self._genesis_id:
                    break
                cur = block.parent_id
            path.reverse()
            return path

    def transactions_disconnecting(self, old_tip: str, new_tip: str) -> list[Transaction]:
        """链头从 old_tip 切到 new_tip 时，旧分支独有交易（自共同祖先向外的顺序）。

        沿旧链向上走，遇到新链上的块（共同祖先）即停止，收集途中块内的全部交易，
        再按"离共同祖先最近的块在前、块内顺序不变"返回，
        便于调用方按 nonce 顺序把交易重新放回交易池。
        """
        with self._lock:
            on_new: set[str] = set()
            cur = new_tip
            while True:
                on_new.add(cur)
                block = self._blocks[cur]
                if cur == self._genesis_id:
                    break
                cur = block.parent_id
            blocks: list[Block] = []
            cur = old_tip
            while cur not in on_new:
                blocks.append(self._blocks[cur])
                cur = self._blocks[cur].parent_id
            blocks.reverse()
            txs: list[Transaction] = []
            for block in blocks:
                txs.extend(block.txs)
            return txs

    def replay_state(self, block_id: str, policy: BlockApplyPolicy) -> LedgerState:
        """从创世开始按路径重放所有交易，得到该块之后的状态。

        与入链时保存的快照独立计算，两者应完全一致。
        """
        path = self.path_from_genesis(block_id)
        genesis_alloc = self._snapshots[self._genesis_id]  # 只用于读总量/余额
        state = LedgerState(
            {
                name: acct.balance
                for name, acct in genesis_alloc._accounts.items()  # noqa: SLF001
            }
        )
        for block in path[1:]:
            for tx in block.txs:
                # 重放时被剔除的交易同样跳过；严格模式下路径上的块都合法
                if self._invalid_reason(state, tx) is None:
                    state.apply_tx(tx, block.producer)
        return state
