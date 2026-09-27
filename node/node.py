"""节点门面：组合账本、交易池与链，对外提供提交/出块/收块能力。"""
from __future__ import annotations

import threading

from .blockchain import AddBlockResult, BlockApplyPolicy, Chain
from .errors import BlockRejected, RejectReason, TxRejected
from .mempool import Mempool
from .models import Block, Transaction

__all__ = ["Node", "BlockApplyPolicy", "AddBlockResult", "BlockRejected", "TxRejected"]


class Node:
    """本地简化链节点。所有公开操作都在同一把锁内串行化。"""

    def __init__(
        self,
        genesis_alloc: dict[str, int] | None = None,
        policy: BlockApplyPolicy = BlockApplyPolicy.REJECT_WHOLE_BLOCK,
        max_block_txs: int = 100,
    ) -> None:
        self._lock = threading.RLock()
        self._chain = Chain(genesis_alloc or {})
        self._pool = Mempool()
        self._policy = policy
        self._max_block_txs = max_block_txs

    @property
    def chain(self) -> Chain:
        return self._chain

    def submit_tx(self, tx: Transaction) -> str:
        """校验并入池，返回 tx_id；非法输入抛 TxRejected（可区分原因）。

        校验顺序：字段 -> 已上链(nonce 过低) -> 断层 -> 余额 -> 池内重复。
        任何一项失败都不会改动池子。
        """
        with self._lock:
            if not tx.basic_valid():
                raise TxRejected(
                    RejectReason.INVALID_TRANSACTION,
                    "金额必须为正、手续费/nonce 非负、账户名非空",
                )
            state = self._chain.head_state()
            onchain = state.get(tx.sender)
            pending_nonces = {t.nonce for t in self._pool.pending_for(tx.sender)}
            expected = onchain.nonce + len(pending_nonces)
            if tx.nonce < onchain.nonce:
                raise TxRejected(
                    RejectReason.NONCE_TOO_LOW,
                    f"nonce={tx.nonce} 已上链，当前账户 nonce={onchain.nonce}",
                )
            if tx.nonce > expected:
                raise TxRejected(
                    RejectReason.NONCE_GAP,
                    f"序号断层: 提交 nonce={tx.nonce}, 下一个期望 {expected}",
                )
            # 到这里 tx.nonce ∈ [onchain.nonce, expected]：
            # 若等于 expected 则是新槽位，否则槽位已被占用，add 会拒绝。
            committed = onchain.balance - self._pool.pending_cost_for(tx.sender)
            if committed < tx.amount + tx.fee:
                raise TxRejected(
                    RejectReason.INSUFFICIENT_FUNDS,
                    f"可用余额 {committed}（已扣除池中挂账）< 需要 {tx.amount + tx.fee}",
                )
            self._pool.add(tx)  # 重复/冲突在此抛 DUPLICATE_TX
            return tx.tx_id

    def produce_block(self, producer: str) -> Block:
        """按稳定规则从交易池选交易打包出块并接到链头。

        自选块一定全部合法，因此以 REJECT_WHOLE_BLOCK 严格接入；
        接入后从池中移除已打包交易，并清理因链头变化而过期的交易。
        """
        with self._lock:
            parent = self._chain.head
            pre = self._chain.head_state()
            # 选交易时只在工作副本上判断；手续费归属在真正接入时计算。
            chosen: list[Transaction] = []
            chosen_ids: set[str] = set()
            budget = self._max_block_txs
            while budget > 0:
                progressed = False
                for cand in self._pool.candidates():
                    if cand.tx_id in chosen_ids:
                        continue
                    if pre.can_apply(cand):
                        chosen.append(cand)
                        chosen_ids.add(cand.tx_id)
                        # 用 producer 收手续费，保证后续候选的余额判断一致
                        pre.apply_tx(cand, producer)
                        budget -= 1
                        progressed = True
                        if budget == 0:
                            break
                if not progressed:
                    break

            block = Block(
                height=parent.height + 1,
                parent_id=self._chain.head_id,
                producer=producer,
                txs=tuple(chosen),
            )
            result = self._chain.add_block(block, BlockApplyPolicy.REJECT_WHOLE_BLOCK)
            self._pool.remove_many([t.tx_id for t in chosen])
            self._prune_pool_after_head_change()
            assert result.block_id == block.block_id
            return block

    def add_block(self, block: Block) -> AddBlockResult:
        """接收外部区块；其权重超过当前链头时切换并回放（快照即回放结果）。

        切换时：被切走分支独有块里的交易重新放回交易池（已在新链落地的除外），
        之后还能被重新打包；这些交易在状态里只会计入一次。
        """
        with self._lock:
            old_head_id = self._chain.head_id
            result = self._chain.add_block(block, self._policy)
            if result.switched_head:
                state = self._chain.head_state()
                for tx in self._chain.transactions_disconnecting(
                    old_head_id, result.block_id
                ):
                    if tx.nonce < state.get(tx.sender).nonce:
                        continue  # 该笔已在新链上落地，不能回池
                    try:
                        self._pool.add(tx)
                    except TxRejected:
                        pass  # 池中已有同笔或同 (sender,nonce) 交易，保留现有交易
                self._prune_pool_after_head_change()
            return result

    def _prune_pool_after_head_change(self) -> None:
        """链头可能切换：池中已在新链头落地的交易移除（nonce 已过期）。

        被切走分支上、新链上未落地的交易保留，供后续重新打包。
        """
        state = self._chain.head_state()
        stale: list[str] = []
        for cand in self._pool.candidates():
            if cand.nonce < state.get(cand.sender).nonce:
                stale.append(cand.tx_id)
        self._pool.remove_many(stale)

    def balance_of(self, account: str) -> int:
        with self._lock:
            return self._chain.head_state().get(account).balance

    def nonce_of(self, account: str) -> int:
        with self._lock:
            return self._chain.head_state().get(account).nonce

    def total_supply(self) -> int:
        with self._lock:
            return self._chain.head_state().total_supply()

    def pending_count(self) -> int:
        return len(self._pool)
