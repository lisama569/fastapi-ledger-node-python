"""错误原因分类与统一异常类型。"""
from __future__ import annotations

import enum


class RejectReason(enum.StrEnum):
    """所有拒绝原因，取值即为对外可区分的稳定字符串。"""

    BAD_INPUT = "bad_input"
    UNKNOWN_SENDER = "unknown_sender"
    DUPLICATE_TX = "duplicate_tx"
    NONCE_STALE = "nonce_stale"
    NONCE_GAP = "nonce_gap"
    NONCE_CONFLICT = "nonce_conflict"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    UNKNOWN_PARENT = "unknown_parent"
    DUPLICATE_BLOCK = "duplicate_block"
    BAD_BLOCK_HASH = "bad_block_hash"
    BAD_BLOCK_STRUCTURE = "bad_block_structure"
    DUPLICATE_TX_IN_BLOCK = "duplicate_tx_in_block"
    ALREADY_APPLIED = "already_applied"
    BLOCK_HAS_INVALID_TX = "block_has_invalid_tx"


class LedgerError(Exception):
    """带可区分原因的业务异常。"""

    def __init__(
        self,
        reason: RejectReason,
        message: str = "",
        details: dict | None = None,
    ) -> None:
        super().__init__(message or str(reason))
        self.reason = reason
        self.message = message or str(reason)
        self.details = details or {}
