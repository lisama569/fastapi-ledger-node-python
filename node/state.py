"""世界状态：账户余额与序号，以及单笔交易的原子应用。"""
from __future__ import annotations

import dataclasses

from .errors import LedgerError, RejectReason
from .models import Account, Transaction


@dataclasses.dataclass(slots=True)
class WorldState:
    """内存世界状态。所有改动要么整体生效、要么抛异常不留半成品。"""

    accounts: dict[str, Account] = dataclasses.field(default_factory=dict)

    @classmethod
    def genesis(cls, allocations: dict[str, int] | None = None) -> "WorldState":
        allocations = allocations or {}
        accounts: dict[str, Account] = {}
        for name, balance in allocations.items():
            if not isinstance(name, str) or not name:
                raise LedgerError(RejectReason.BAD_INPUT, "账户名必须是非空字符串")
            if not isinstance(balance, int) or isinstance(balance, bool) or balance < 0:
                raise LedgerError(RejectReason.BAD_INPUT, "初始余额必须是 >=0 的整数")
            accounts[name] = Account(balance=balance, nonce=0)
        return cls(accounts=accounts)

    def snapshot(self) -> "WorldState":
        # Account 是不可变对象，复制一层 dict 即可互不影响
        return WorldState(accounts=dict(self.accounts))

    def get_balance(self, name: str) -> int:
        acc = self.accounts.get(name)
        return 0 if acc is None else acc.balance

    def get_nonce(self, name: str) -> int:
        acc = self.accounts.get(name)
        return 0 if acc is None else acc.nonce

    def total_supply(self) -> int:
        return sum(acc.balance for acc in self.accounts.values())

    def ensure_account(self, name: str) -> None:
        if name not in self.accounts:
            self.accounts[name] = Account(balance=0, nonce=0)

    def apply_tx(self, tx: Transaction, fee_recipient: str) -> None:
        """原子应用单笔交易：余额、序号、手续费要么一起成功，要么不动。"""
        if not isinstance(fee_recipient, str) or not fee_recipient:
            raise LedgerError(RejectReason.BAD_INPUT, "fee_recipient 必须是非空字符串")

        sender = self.accounts.get(tx.sender)
        if sender is None:
            raise LedgerError(
                RejectReason.UNKNOWN_SENDER,
                f"发送方账户不存在: {tx.sender}",
                {"sender": tx.sender, "tx_id": tx.tx_id},
            )
        if tx.nonce < sender.nonce:
            raise LedgerError(
                RejectReason.NONCE_STALE,
                f"序号已用过: tx.nonce={tx.nonce}, account_nonce={sender.nonce}",
                {"tx_id": tx.tx_id, "expected": sender.nonce, "got": tx.nonce},
            )
        if tx.nonce > sender.nonce:
            raise LedgerError(
                RejectReason.NONCE_GAP,
                f"序号断层: tx.nonce={tx.nonce}, expected={sender.nonce}",
                {"tx_id": tx.tx_id, "expected": sender.nonce, "got": tx.nonce},
            )
        cost = tx.cost()
        if sender.balance < cost:
            raise LedgerError(
                RejectReason.INSUFFICIENT_BALANCE,
                f"余额不足: balance={sender.balance}, need={cost}",
                {
                    "tx_id": tx.tx_id,
                    "balance": sender.balance,
                    "amount": tx.amount,
                    "fee": tx.fee,
                },
            )

        # 在工作副本上完成全部改动，最后一步才替换，保证原子性
        working = dict(self.accounts)
        working[tx.sender] = Account(
            balance=sender.balance - cost, nonce=sender.nonce + 1
        )
        recipient = working.get(tx.recipient, Account(0, 0))
        working[tx.recipient] = Account(
            balance=recipient.balance + tx.amount, nonce=recipient.nonce
        )
        beneficiary = working.get(fee_recipient, Account(0, 0))
        working[fee_recipient] = Account(
            balance=beneficiary.balance + tx.fee, nonce=beneficiary.nonce
        )
        self.accounts = working
