"""交易提交：序号/手续费/余额校验、重复提交、并发不乱序。"""
from __future__ import annotations

import threading

import pytest

from conftest import show_tx
from node.errors import TxRejected
from node.models import Transaction
from node.node import BlockApplyPolicy, Node

GENESIS = {"alice": 1000, "bob": 100, "carol": 0}


def make_node() -> Node:
    return Node(GENESIS, policy=BlockApplyPolicy.REJECT_WHOLE_BLOCK)


def test_valid_tx_accepted() -> None:
    node = make_node()
    tx = Transaction("alice", "bob", 0, 100, 5)
    show_tx("提交合法交易", tx)
    tx_id = node.submit_tx(tx)
    print(f"判定: 接受, tx_id={tx_id[:8]}, 池大小={node.pending_count()}")
    assert node.pending_count() == 1
    assert node.balance_of("alice") == 1000  # 未出块，余额不变


@pytest.mark.parametrize(
    "tx,expected",
    [
        (Transaction("alice", "bob", 0, 0, 5), "INVALID_TRANSACTION"),       # 金额为 0
        (Transaction("alice", "bob", 0, 100, -1), "INVALID_TRANSACTION"),    # 负手续费
        (Transaction("alice", "bob", -1, 100, 5), "INVALID_TRANSACTION"),    # 负 nonce
        (Transaction("", "bob", 0, 100, 5), "INVALID_TRANSACTION"),          # 空付款方
        (Transaction("alice", "bob", 5, 100, 5), "NONCE_GAP"),               # 序号断层
        (Transaction("alice", "bob", 0, 10000, 5), "INSUFFICIENT_FUNDS"),    # 余额不足
        (Transaction("carol", "bob", 0, 1, 0), "INSUFFICIENT_FUNDS"),        # 零余额
    ],
)
def test_reject_reasons_are_distinguishable(tx: Transaction, expected: str) -> None:
    node = make_node()
    show_tx("非法输入", tx)
    with pytest.raises(TxRejected) as exc:
        node.submit_tx(tx)
    print(f"判定依据: 期望={expected} 实际={exc.value.reason.value} 说明={exc.value.detail}")
    assert exc.value.reason.value == expected
    assert node.pending_count() == 0


def test_duplicate_and_conflict_never_overwrite() -> None:
    node = make_node()
    tx = Transaction("alice", "bob", 0, 100, 5)
    node.submit_tx(tx)
    show_tx("重复提交同一笔", tx)
    with pytest.raises(TxRejected) as exc1:
        node.submit_tx(tx)
    print(f"判定依据(同笔): {exc1.value.reason.value} - {exc1.value.detail}")
    assert exc1.value.reason.value == "DUPLICATE_TX"

    # 同 sender+nonce 但内容不同：不能顶掉池里已有的
    conflict = Transaction("alice", "carol", 0, 200, 9)
    show_tx("同 nonce 冲突交易", conflict)
    with pytest.raises(TxRejected) as exc2:
        node.submit_tx(conflict)
    print(f"判定依据(冲突): {exc2.value.reason.value} - {exc2.value.detail}")
    assert exc2.value.reason.value == "DUPLICATE_TX"
    assert node.pending_count() == 1  # 原有交易仍在，未被顶掉
    node.produce_block("miner")
    # 出块后 nonce 已上链，再提交旧 nonce -> NONCE_TOO_LOW
    show_tx("已上链后再提交旧 nonce", tx)
    with pytest.raises(TxRejected) as exc3:
        node.submit_tx(tx)
    print(f"判定依据(已上链): {exc3.value.reason.value} - {exc3.value.detail}")
    assert exc3.value.reason.value == "NONCE_TOO_LOW"


def test_nonce_gap_does_not_block_later_valid_submission() -> None:
    node = make_node()
    gapped = Transaction("alice", "bob", 3, 10, 0)
    show_tx("断层交易(nonce=3)", gapped)
    with pytest.raises(TxRejected) as exc:
        node.submit_tx(gapped)
    print(f"判定依据: {exc.value.reason.value}")
    ok = Transaction("alice", "bob", 0, 10, 0)
    show_tx("随后提交 nonce=0", ok)
    node.submit_tx(ok)
    assert node.pending_count() == 1


def test_balance_check_counts_pending_txs() -> None:
    """池中挂账要计入：单笔都够、两笔合计不够时，第二笔必须拒绝。"""
    node = make_node()
    first = Transaction("bob", "alice", 0, 60, 0)   # bob 有 100
    second = Transaction("bob", "alice", 1, 60, 0)  # 两笔合计 120 > 100
    node.submit_tx(first)
    show_tx("提交第二笔（合计超额）", second)
    with pytest.raises(TxRejected) as exc:
        node.submit_tx(second)
    print(f"判定依据: {exc.value.reason.value} - {exc.value.detail}")
    assert exc.value.reason.value == "INSUFFICIENT_FUNDS"
    assert node.pending_count() == 1


def test_concurrent_submissions_keep_queue_consistent() -> None:
    """多线程并发提交：不丢失、不覆盖、不写乱，拒绝原因可区分。"""
    node = make_node()
    accepted: list[str] = []
    rejected: list[tuple[str, str]] = []
    lock = threading.Lock()

    def worker(sender: str, nonces: list[int]) -> None:
        for nonce in nonces:
            tx = Transaction(sender, "bob", nonce, 10, nonce)
            try:
                tx_id = node.submit_tx(tx)
                with lock:
                    accepted.append((sender, nonce, tx_id))
            except TxRejected as exc:
                with lock:
                    rejected.append((tx.tx_id, exc.reason.value))

    threads = []
    # 每个账户 10 个线程各提交同一份 nonce 序列 0..9：
    # 每个槽位恰好一笔被接受，其余全部 DUPLICATE_TX；
    # bob 余额有限，后面的槽位还会因 INSUFFICIENT_FUNDS/NONCE_GAP 被拒。
    for sender in ("alice", "bob"):
        for _ in range(10):
            threads.append(threading.Thread(target=worker, args=(sender, list(range(10)))))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    by_sender: dict[str, set[int]] = {}
    tx_ids: set[str] = set()
    for sender, nonce, tx_id in accepted:
        # 同一 (sender, nonce) 绝不能被接受两次
        assert nonce not in by_sender.setdefault(sender, set()), (sender, nonce)
        by_sender[sender].add(nonce)
        assert tx_id not in tx_ids
        tx_ids.add(tx_id)
    print(f"并发结果: 接受 {len(accepted)} 笔, 拒绝 {len(rejected)} 笔")
    print(f"各账户接受的 nonce: alice={sorted(by_sender.get('alice', set()))} "
          f"bob={sorted(by_sender.get('bob', set()))}")
    print("拒绝原因分布:", {r: sum(1 for _, x in rejected if x == r) for r in
          {x for _, x in rejected}})
    # 接受的 nonce 必须是从 0 开始的连续前缀（断层交易永远不会被接受）
    for sender, nonces in by_sender.items():
        assert sorted(nonces) == list(range(len(nonces))), sender
    assert set(reason for _, reason in rejected) <= {
        "DUPLICATE_TX",
        "INSUFFICIENT_FUNDS",
        "NONCE_GAP",
    }
    # 池大小与接受数一致，出块成功且总量守恒
    assert node.pending_count() == len(accepted)
    block = node.produce_block("miner")
    print(f"出块成功: 高度={block.height}, 打包 {len(block.txs)} 笔")
    assert node.total_supply() == 1100
