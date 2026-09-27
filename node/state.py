"""账本状态：账户余额/nonce、总量校验、交易应用与快照。"""
from __future__ import annotations

from .models import AccountState, Transaction


class LedgerState:
    """账户账本。缺失账户视为 balance=0, nonce=0。

    不变量：total_supply 只由创世分配决定；转账与手续费只在账户间移动，
    应用交易前后总量不变。
    """

    def __init__(self, genesis_alloc: dict[str, int] | None = None) -> None:
        self._accounts: dict[str, AccountState] = {}
        for account, amount in (genesis_alloc or {}).items():
            self._accounts[account] = AccountState(balance=amount, nonce=0)

    def get(self, account: str) -> AccountState:
        """读取账户状态；不存在的账户返回零值副本（不写回）。"""
        st = self._accounts.get(account)
        if st is None:
            return AccountState(balance=0, nonce=0)
        return AccountState(balance=st.balance, nonce=st.nonce)

    def _mut(self, account: str) -> AccountState:
        return self._accounts.setdefault(account, AccountState())

    def total_supply(self) -> int:
        return sum(st.balance for st in self._accounts.values())

    def can_apply(self, tx: Transaction) -> bool:
        """在当前状态下能否应用：nonce 精确匹配且余额够付 amount+fee。"""
        st = self.get(tx.sender)
        return st.nonce == tx.nonce and st.balance >= tx.amount + tx.fee

    def apply_tx(self, tx: Transaction, fee_recipient: str) -> None:
        """应用一笔已判定合法的交易。

        先校验再改动，校验不过抛 AssertionError，状态保持原样，
        不会出现"扣了一半"。
        """
        assert tx.basic_valid(), f"非法交易字段: {tx}"
        sender = self._mut(tx.sender)
        assert sender.nonce == tx.nonce, (
            f"nonce 不匹配: 期望 {sender.nonce}, 实际 {tx.nonce}"
        )
        assert sender.balance >= tx.amount + tx.fee, (
            f"余额不足: 需 {tx.amount + tx.fee}, 有 {sender.balance}"
        )
        sender.balance -= tx.amount + tx.fee
        sender.nonce += 1
        self._mut(tx.recipient).balance += tx.amount
        if fee_recipient:
            self._mut(fee_recipient).balance += tx.fee

    def snapshot(self) -> "LedgerState":
        """深拷贝当前状态，用于按块保存快照与分叉回放。"""
        cp = LedgerState()
        cp._accounts = {
            name: AccountState(balance=st.balance, nonce=st.nonce)
            for name, st in self._accounts.items()
        }
        return cp
