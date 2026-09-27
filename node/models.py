"""数据类型定义：交易、账户、区块、执行结果与编码。

本地简化链不做真实密码学：区块"身份"用对内容做 SHA-256 摘要来模拟，
只用来判断"同一份块"（重复送进来必须幂等）。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json

from .errors import LedgerError, RejectReason


def _is_real_int(v) -> bool:
    """bool 是 int 的子类，但本协议不接受 true/false 当数字。"""
    return isinstance(v, int) and not isinstance(v, bool)


@dataclasses.dataclass(frozen=True, slots=True)
class Transaction:
    """一笔简单转账交易。

    - sender / recipient: 账户名（本地字符串）
    - nonce:   发送方账户序号，必须严格等于该账户已用序号
    - amount:  转账金额
    - fee:     手续费（在本块被受益人收取）
    - tx_id:   客户端给出的交易标识，用于识别重复提交
    """

    sender: str
    recipient: str
    nonce: int
    amount: int
    fee: int
    tx_id: str

    def cost(self) -> int:
        return self.amount + self.fee

    def to_canonical_dict(self) -> dict:
        return {
            "sender": self.sender,
            "recipient": self.recipient,
            "nonce": self.nonce,
            "amount": self.amount,
            "fee": self.fee,
            "tx_id": self.tx_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transaction":
        if not isinstance(data, dict):
            raise LedgerError(RejectReason.BAD_INPUT, "交易必须是 JSON 对象")
        required = ("sender", "recipient", "nonce", "amount", "fee", "tx_id")
        missing = [k for k in required if k not in data]
        if missing:
            raise LedgerError(
                RejectReason.BAD_INPUT, f"缺少字段: {','.join(missing)}"
            )
        sender = data["sender"]
        recipient = data["recipient"]
        tx_id = data["tx_id"]
        nonce = data["nonce"]
        amount = data["amount"]
        fee = data["fee"]
        if not isinstance(sender, str) or not sender:
            raise LedgerError(RejectReason.BAD_INPUT, "sender 必须是非空字符串")
        if not isinstance(recipient, str) or not recipient:
            raise LedgerError(RejectReason.BAD_INPUT, "recipient 必须是非空字符串")
        if not isinstance(tx_id, str) or not tx_id:
            raise LedgerError(RejectReason.BAD_INPUT, "tx_id 必须是非空字符串")
        if not _is_real_int(nonce) or nonce < 0:
            raise LedgerError(RejectReason.BAD_INPUT, "nonce 必须是 >=0 的整数")
        if not _is_real_int(amount) or amount < 0:
            raise LedgerError(RejectReason.BAD_INPUT, "amount 必须是 >=0 的整数")
        if not _is_real_int(fee) or fee < 0:
            raise LedgerError(RejectReason.BAD_INPUT, "fee 必须是 >=0 的整数")
        return cls(
            sender=sender,
            recipient=recipient,
            nonce=nonce,
            amount=amount,
            fee=fee,
            tx_id=tx_id,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class Account:
    """账户状态：余额与下一个可用序号。"""

    balance: int
    nonce: int


@dataclasses.dataclass(frozen=True, slots=True)
class TxReceipt:
    """单笔交易在某次执行中的处理结果。"""

    tx_id: str
    accepted: bool
    reason: RejectReason | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "tx_id": self.tx_id,
            "accepted": self.accepted,
            "reason": None if self.reason is None else str(self.reason),
            "detail": self.detail,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class Block:
    """区块。block_id 为对内容的 SHA-256 摘要（在 build_block 中计算）。"""

    height: int
    parent_id: str
    producer: str
    transactions: tuple[Transaction, ...]
    weight: int = 1
    block_id: str = ""

    def to_canonical_dict(self) -> dict:
        return block_content_dict(self)


GENESIS_ID = "GENESIS"
ROOT_PARENT = "ROOT"


@dataclasses.dataclass(frozen=True, slots=True)
class BlockResult:
    """一个块在指定父状态上执行的结果。"""

    block_id: str
    accepted: bool
    policy: str
    included: tuple[TxReceipt, ...] = ()
    excluded: tuple[TxReceipt, ...] = ()
    reject_reason: RejectReason | None = None
    detail: str = ""


def block_content_dict(b: Block) -> dict:
    """参与摘要的规范内容：不含 block_id 本身。"""
    return {
        "height": b.height,
        "parent_id": b.parent_id,
        "producer": b.producer,
        "weight": b.weight,
        "transactions": [t.to_canonical_dict() for t in b.transactions],
    }


def block_digest(b: Block) -> str:
    payload = json.dumps(
        block_content_dict(b), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def genesis_block() -> Block:
    return Block(
        height=0,
        parent_id=ROOT_PARENT,
        producer="genesis",
        transactions=(),
        weight=0,
        block_id=GENESIS_ID,
    )


def build_block(
    *,
    height: int,
    parent_id: str,
    producer: str,
    transactions: tuple[Transaction, ...] | list[Transaction],
    weight: int = 1,
) -> Block:
    if not _is_real_int(height) or height <= 0:
        raise LedgerError(RejectReason.BAD_INPUT, "height 必须是正整数")
    if not isinstance(parent_id, str) or not parent_id:
        raise LedgerError(RejectReason.BAD_INPUT, "parent_id 必须是非空字符串")
    if not isinstance(producer, str) or not producer:
        raise LedgerError(RejectReason.BAD_INPUT, "producer 必须是非空字符串")
    if not _is_real_int(weight) or weight <= 0:
        raise LedgerError(RejectReason.BAD_INPUT, "weight 必须是正整数")
    txs = tuple(transactions)
    if not all(isinstance(t, Transaction) for t in txs):
        raise LedgerError(RejectReason.BAD_INPUT, "transactions 必须是交易列表")
    base = Block(
        height=height,
        parent_id=parent_id,
        producer=producer,
        transactions=txs,
        weight=weight,
        block_id="",
    )
    return dataclasses.replace(base, block_id=block_digest(base))


def block_from_dict(data: dict) -> Block:
    """从网络 JSON 重建块，并强制重新计算摘要比对（防篡改/防错配）。"""
    if not isinstance(data, dict):
        raise LedgerError(RejectReason.BAD_INPUT, "区块必须是 JSON 对象")
    required = ("height", "parent_id", "producer", "transactions")
    missing = [k for k in required if k not in data]
    if missing:
        raise LedgerError(
            RejectReason.BAD_BLOCK_STRUCTURE, f"区块缺少字段: {','.join(missing)}"
        )
    try:
        raw_txs = data["transactions"]
        if not isinstance(raw_txs, list):
            raise LedgerError(
                RejectReason.BAD_BLOCK_STRUCTURE, "transactions 必须是列表"
            )
        txs = tuple(Transaction.from_dict(t) for t in raw_txs)
        block = build_block(
            height=data["height"],
            parent_id=data["parent_id"],
            producer=data["producer"],
            transactions=txs,
            weight=int(data.get("weight", 1)),
        )
    except LedgerError:
        raise
    except Exception as exc:  # 重建阶段的任何结构问题都归类
        raise LedgerError(RejectReason.BAD_BLOCK_STRUCTURE, str(exc)) from exc
    claimed = data.get("block_id")
    if claimed is not None and claimed != block.block_id:
        raise LedgerError(
            RejectReason.BAD_BLOCK_HASH,
            f"区块摘要不匹配: 声称 {claimed}，实际 {block.block_id}",
            {"claimed": claimed, "actual": block.block_id},
        )
    return block
