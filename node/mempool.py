"""待处理交易池：校验、去重与稳定的出块选交易规则。"""
from __future__ import annotations

import dataclasses

from .errors import LedgerError, RejectReason
from .models import Transaction
from .state import WorldState


@dataclasses.dataclass(slots=True)
class Mempool:
    """以"下一个可执行序号"为槽位存放待处理交易。

    每个账户在池中每个序号最多保留一笔交易；同序号不同 tx_id 的交易
    不能顶掉已有交易（返回 NONCE_CONFLICT），同一笔重复提交返回
    DUPLICATE_TX，二者原因可区分。
    """

    state: WorldState
    pending: dict[tuple[str, int], Transaction] = dataclasses.field(
        default_factory=dict
    )
    rejected: list[tuple[Transaction | None, RejectReason, str]] = dataclasses.field(
        default_factory=list
    )

    # ------------------------------------------------------------------ submit
    def _sender_pending_nonces(self, sender: str) -> list[int]:
        return sorted(n for (s, n) in self.pending if s == sender)

    def submit(self, tx: Transaction) -> Transaction:
        """提交前校验。成功返回该交易；任何失败都不改动池子。"""
        account = self.state.accounts.get(tx.sender)
        if account is None:
            raise LedgerError(
                RejectReason.UNKNOWN_SENDER,
                f"发送方账户不存在: {tx.sender}",
                {"sender": tx.sender, "tx_id": tx.tx_id},
            )

        existing = self.pending.get((tx.sender, tx.nonce))
        if existing is not None:
            if existing.tx_id == tx.tx_id:
                raise LedgerError(
                    RejectReason.DUPLICATE_TX,
                    f"交易已在待处理队列中: {tx.tx_id}",
                    {"tx_id": tx.tx_id, "sender": tx.sender, "nonce": tx.nonce},
                )
            raise LedgerError(
                RejectReason.NONCE_CONFLICT,
                f"该序号槽位已被另一笔交易占用: "
                f"{tx.sender} nonce={tx.nonce} occupied_by={existing.tx_id}",
                {
                    "tx_id": tx.tx_id,
                    "occupied_by": existing.tx_id,
                    "sender": tx.sender,
                    "nonce": tx.nonce,
                },
            )

        if tx.nonce < account.nonce:
            raise LedgerError(
                RejectReason.NONCE_STALE,
                f"序号已用过: tx.nonce={tx.nonce}, account_nonce={account.nonce}",
                {"tx_id": tx.tx_id, "expected": account.nonce, "got": tx.nonce},
            )
        queued_nonces = self._sender_pending_nonces(tx.sender)
        expected_nonce = account.nonce + len(queued_nonces)
        if tx.nonce > expected_nonce:
            raise LedgerError(
                RejectReason.NONCE_GAP,
                f"序号断层: tx.nonce={tx.nonce}, 下一个可提交序号={expected_nonce}",
                {
                    "tx_id": tx.tx_id,
                    "expected": expected_nonce,
                    "got": tx.nonce,
                    "queued_nonces": queued_nonces,
                },
            )

        # 余额检查：账户初始余额必须覆盖"池中该账户更早序号交易" + 本笔的花费。
        earlier_cost = sum(
            self.pending[(tx.sender, n)].cost()
            for n in queued_nonces
            if n < tx.nonce
        )
        need = earlier_cost + tx.cost()
        if account.balance < need:
            raise LedgerError(
                RejectReason.INSUFFICIENT_BALANCE,
                f"余额不足（含队列中更早序号交易）: balance={account.balance}, need={need}",
                {
                    "tx_id": tx.tx_id,
                    "balance": account.balance,
                    "queued_earlier_cost": earlier_cost,
                    "amount": tx.amount,
                    "fee": tx.fee,
                },
            )

        self.pending[(tx.sender, tx.nonce)] = tx
        return tx

    def remove_included(
        self, txs: list[Transaction] | tuple[Transaction, ...]
    ) -> None:
        for tx in txs:
            self.pending.pop((tx.sender, tx.nonce), None)

    # ------------------------------------------------------------ block picking
    def ordered_candidates(self) -> list[Transaction]:
        """出块选交易的全局顺序（规则本身）：
        1) 手续费 fee 高者优先；
        2) fee 相同则 tx_id 字典序小者优先（保证确定性）。
        """
        return sorted(self.pending.values(), key=lambda t: (-t.fee, t.tx_id))

    def select_for_block(self, fee_recipient: str) -> list[Transaction]:
        """按 ordered_candidates 顺序做一次贪心扫描：

        - 在父状态的工作副本上逐笔试应用，成功则收录；
        - 试应用失败（序号断层/已用、余额不足、账户不存在）就跳过并留在池里，
          等下一个块；
        - 同一账户序号因此天然连续，余额也按"本块内累计花费"判定。
        """
        view = self.state.snapshot()
        chosen: list[Transaction] = []
        for tx in self.ordered_candidates():
            try:
                view.apply_tx(tx, fee_recipient)
            except LedgerError:
                continue
            chosen.append(tx)
        return chosen

    # ------------------------------------------------------------- reorg cleanup
    def rebuild_after_switch(
        self, new_tip_state: WorldState
    ) -> list[tuple[str, RejectReason, str]]:
        """链头切换后，用新链头状态重校池中交易。

        按 (sender, nonce) 顺序在工作副本上逐笔试应用：能应用的保留，
        不能应用的丢弃并记录可区分原因。一旦某序号被丢，该账户后续序号
        自然变成断层，会一并被丢。
        """
        self.state = new_tip_state
        # 只做"可应用性"校验（序号、余额），不真的入账，避免凭空产生手续费账户
        sim = new_tip_state.snapshot()
        kept: dict[tuple[str, int], Transaction] = {}
        dropped: list[tuple[str, RejectReason, str]] = []
        for key in sorted(self.pending.keys()):
            tx = self.pending[key]
            try:
                sim.apply_tx(tx, tx.sender)  # 受益人取发送方，不新增账户
            except LedgerError as exc:
                dropped.append((tx.tx_id, exc.reason, exc.message))
                continue
            kept[key] = tx
        self.pending = kept
        return dropped
