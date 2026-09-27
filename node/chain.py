"""区块存储、状态快照、块执行、链头切换与重放校验。"""
from __future__ import annotations

import dataclasses

from .errors import LedgerError, RejectReason
from .models import (
    GENESIS_ID,
    Block,
    BlockResult,
    TxReceipt,
    block_digest,
    genesis_block,
)
from .state import WorldState

STRICT = "strict"
SKIP = "skip"


@dataclasses.dataclass(slots=True)
class Blockchain:
    """以 block_id 为主键存块、以 block_id 存执行后状态快照。

    分叉块全部保留（被切走的分支状态也能找回）；链头按累计 weight 比较，
    权重相同不切换（先到为准）。
    """

    blocks: dict[str, Block] = dataclasses.field(default_factory=dict)
    states: dict[str, WorldState] = dataclasses.field(default_factory=dict)
    # block_id -> 该块的执行结果（skip 策略下记录哪些交易实际被收录）
    results: dict[str, BlockResult] = dataclasses.field(default_factory=dict)
    tip_id: str = GENESIS_ID
    genesis_allocations: dict[str, int] = dataclasses.field(default_factory=dict)

    @classmethod
    def create(cls, allocations: dict[str, int] | None = None) -> "Blockchain":
        chain = cls(genesis_allocations=dict(allocations or {}))
        chain.blocks[GENESIS_ID] = genesis_block()
        chain.states[GENESIS_ID] = WorldState.genesis(allocations)
        return chain

    # ------------------------------------------------------------- basic views
    @property
    def tip(self) -> Block:
        return self.blocks[self.tip_id]

    @property
    def tip_state(self) -> WorldState:
        return self.states[self.tip_id]

    def get_block(self, block_id: str) -> Block | None:
        return self.blocks.get(block_id)

    def has_block(self, block_id: str) -> bool:
        return block_id in self.blocks

    def state_at(self, block_id: str) -> WorldState:
        if block_id not in self.states:
            raise LedgerError(
                RejectReason.BAD_INPUT, f"未知区块，无状态快照: {block_id}"
            )
        return self.states[block_id]

    def chain_height(self) -> int:
        return self.tip.height

    def chain_weight(self, block_id: str) -> int:
        total = 0
        cur = self.blocks.get(block_id)
        while cur is not None:
            total += cur.weight
            if cur.block_id == GENESIS_ID:
                break
            cur = self.blocks.get(cur.parent_id)
        return total

    def chain_ids_back(self, block_id: str) -> list[str]:
        """从指定块沿 parent_id 回到创世块，返回创世在前的 id 列表。"""
        ids: list[str] = []
        cur = self.blocks.get(block_id)
        seen: set[str] = set()
        while cur is not None:
            if cur.block_id in seen:
                raise LedgerError(
                    RejectReason.BAD_BLOCK_STRUCTURE,
                    f"父链出现环: {cur.block_id}",
                )
            seen.add(cur.block_id)
            ids.append(cur.block_id)
            if cur.block_id == GENESIS_ID:
                break
            cur = self.blocks.get(cur.parent_id)
        ids.reverse()
        return ids

    def active_ids(self) -> list[str]:
        return self.chain_ids_back(self.tip_id)

    def _txs_on_chain(self, head_id: str) -> set:
        txs: set = set()
        for bid in self.chain_ids_back(head_id):
            blk = self.blocks[bid]
            for tx in blk.transactions:
                txs.add((tx.sender, tx.nonce, tx.tx_id))
        return txs

    # ------------------------------------------------------------ block execute
    def _validate_structure(self, block: Block) -> None:
        if block.block_id != block_digest(block):
            raise LedgerError(
                RejectReason.BAD_BLOCK_HASH,
                f"区块摘要不匹配: {block.block_id}",
                {"block_id": block.block_id},
            )
        if block.block_id in self.blocks:
            raise LedgerError(
                RejectReason.DUPLICATE_BLOCK,
                f"区块已存在（重复送进来不会重复计入）: {block.block_id}",
                {"block_id": block.block_id},
            )
        parent = self.blocks.get(block.parent_id)
        if parent is None:
            raise LedgerError(
                RejectReason.UNKNOWN_PARENT,
                f"父块未知: {block.parent_id}",
                {"parent_id": block.parent_id},
            )
        if block.height != parent.height + 1:
            raise LedgerError(
                RejectReason.BAD_BLOCK_STRUCTURE,
                f"高度不连续: block.height={block.height}, parent.height={parent.height}",
                {"height": block.height, "parent_height": parent.height},
            )
        seen_pairs: set[tuple[str, int]] = set()
        seen_ids: set[str] = set()
        for tx in block.transactions:
            pair = (tx.sender, tx.nonce)
            if pair in seen_pairs or tx.tx_id in seen_ids:
                raise LedgerError(
                    RejectReason.DUPLICATE_TX_IN_BLOCK,
                    f"块内重复交易: tx_id={tx.tx_id} sender={tx.sender} nonce={tx.nonce}",
                    {"tx_id": tx.tx_id, "sender": tx.sender, "nonce": tx.nonce},
                )
            seen_pairs.add(pair)
            seen_ids.add(tx.tx_id)

    def _already_applied_receipts(
        self, block: Block
    ) -> dict[str, TxReceipt]:
        """检查交易是否已经在父链（即将延伸的那条链）上生效过。"""
        on_chain = self._txs_on_chain(block.parent_id)
        receipts: dict[str, TxReceipt] = {}
        for tx in block.transactions:
            if (tx.sender, tx.nonce, tx.tx_id) in on_chain:
                receipts[tx.tx_id] = TxReceipt(
                    tx_id=tx.tx_id,
                    accepted=False,
                    reason=RejectReason.ALREADY_APPLIED,
                    detail=(
                        f"交易已在父链上生效: sender={tx.sender} nonce={tx.nonce}"
                    ),
                )
        return receipts

    def execute_block(
        self, block: Block, policy: str = STRICT
    ) -> tuple[BlockResult, WorldState]:
        """在父块状态快照上执行一个块。

        strict: 任一交易非法 -> 整块拒绝，状态不动（返回 accepted=False 及原因）。
        skip:   逐笔在临时状态上试跑，非法单笔剔除，其余整体提交。
        """
        if policy not in (STRICT, SKIP):
            raise LedgerError(
                RejectReason.BAD_INPUT, f"未知块处理策略: {policy}"
            )

        work = self.states[block.parent_id].snapshot()
        included: list[TxReceipt] = []
        excluded: list[TxReceipt] = []

        pre_rejected = self._already_applied_receipts(block)

        for tx in block.transactions:
            pre = pre_rejected.get(tx.tx_id)
            if pre is not None:
                if policy == STRICT:
                    return (
                        BlockResult(
                            block_id=block.block_id,
                            accepted=False,
                            policy=policy,
                            reject_reason=RejectReason.BLOCK_HAS_INVALID_TX,
                            detail=(
                                "strict 策略：块中含不合法交易，整块拒绝；"
                                f"首笔非法交易 tx_id={tx.tx_id} 原因 already_applied"
                            ),
                            excluded=(pre,),
                        ),
                        self.states[block.parent_id],
                    )
                excluded.append(pre)
                continue
            try:
                work.apply_tx(tx, block.producer)
            except LedgerError as exc:
                receipt = TxReceipt(
                    tx_id=tx.tx_id,
                    accepted=False,
                    reason=exc.reason,
                    detail=exc.message,
                )
                if policy == STRICT:
                    return (
                        BlockResult(
                            block_id=block.block_id,
                            accepted=False,
                            policy=policy,
                            reject_reason=RejectReason.BLOCK_HAS_INVALID_TX,
                            detail=(
                                "strict 策略：块中含不合法交易，整块拒绝；"
                                f"首笔非法交易 tx_id={tx.tx_id} 原因={exc.reason}"
                            ),
                            excluded=(receipt,),
                        ),
                        self.states[block.parent_id],
                    )
                excluded.append(receipt)
                continue
            included.append(
                TxReceipt(tx_id=tx.tx_id, accepted=True, detail="included")
            )

        return (
            BlockResult(
                block_id=block.block_id,
                accepted=True,
                policy=policy,
                included=tuple(included),
                excluded=tuple(excluded),
                detail=(
                    "全部交易已收录"
                    if not excluded
                    else f"skip 策略：{len(excluded)} 笔非法交易被单笔剔除，其余已收录"
                ),
            ),
            work,
        )

    # ------------------------------------------------------------ store / recv
    def commit_block(
        self, block: Block, state: WorldState, result: BlockResult
    ) -> None:
        self.blocks[block.block_id] = block
        self.states[block.block_id] = state
        self.results[block.block_id] = result

    def receive_block(self, block: Block, policy: str = STRICT) -> BlockResult:
        """接收块：结构校验 -> 执行 -> 存块/存快照 -> 必要时切换链头。

        同一份块重复送进来返回 DUPLICATE_BLOCK 且幂等（不重复计入状态）。
        """
        existing = self.blocks.get(block.block_id)
        if existing is not None:
            switched = self.maybe_switch_tip(block.block_id)
            return BlockResult(
                block_id=block.block_id,
                accepted=False,
                policy=policy,
                reject_reason=RejectReason.DUPLICATE_BLOCK,
                detail=(
                    "区块已存在，未重复计入状态"
                    + ("；该块权重更大，已切换链头" if switched else "；链头不变")
                ),
            )
        self._validate_structure(block)
        result, state = self.execute_block(block, policy)
        if not result.accepted:
            # strict 整块拒绝：不存块、不存状态
            return result
        self.commit_block(block, state, result)
        switched = self.maybe_switch_tip(block.block_id)
        if switched:
            result = dataclasses.replace(
                result,
                detail=result.detail + "；权重大于当前链头，已切换链头",
            )
        return result

    # ------------------------------------------------------------------ reorg
    def maybe_switch_tip(self, candidate_id: str) -> bool:
        """仅当候选链累计权重严格更大时切换。"""
        if candidate_id == self.tip_id:
            return False
        if self.chain_weight(candidate_id) > self.chain_weight(self.tip_id):
            self.switch_tip(candidate_id)
            return True
        return False

    def switch_tip(self, new_id: str) -> bool:
        if new_id not in self.blocks:
            raise LedgerError(RejectReason.BAD_INPUT, f"未知区块: {new_id}")
        if new_id == self.tip_id:
            return False
        self.tip_id = new_id
        return True

    # ----------------------------------------------------------------- replay
    def replay_state(self, head_id: str) -> WorldState:
        """不使用任何缓存快照，从创世块开始把每块被收录的交易逐笔重放。

        "被收录"以块里的交易清单为准；配合"重放结果必须与缓存快照一致"
        的校验，可以发现任何重复计入或漏计。
        """
        state = WorldState.genesis(self.genesis_allocations)
        for bid in self.chain_ids_back(head_id):
            if bid == GENESIS_ID:
                continue
            blk = self.blocks[bid]
            result = self.results.get(bid)
            if result is None:
                # strict 块：块内交易全部被收录
                included_ids = {tx.tx_id for tx in blk.transactions}
            else:
                included_ids = {r.tx_id for r in result.included}
            for tx in blk.transactions:
                if tx.tx_id in included_ids:
                    # 跳过校验的纯重放：正常链上收录集必然合法
                    state.apply_tx(tx, blk.producer)
        return state

    def check_replay_consistency(self, head_id: str) -> bool:
        replayed = self.replay_state(head_id)
        cached = self.states[head_id]
        return replayed.accounts == cached.accounts
