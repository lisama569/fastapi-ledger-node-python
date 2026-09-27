"""交易池校验与出块选交易规则测试。"""
from __future__ import annotations

import pytest

from node.errors import LedgerError, RejectReason
from node.mempool import Mempool
from node.state import WorldState


def tx(sender, recipient, nonce, amount, fee, tx_id):
    from node.models import Transaction

    return Transaction(sender, recipient, nonce, amount, fee, tx_id)


def make_pool():
    state = WorldState.genesis({"alice": 100, "bob": 50, "carol": 10})
    return state, Mempool(state=state)


def reject_reason(fn):
    with pytest.raises(LedgerError) as exc:
        fn()
    return exc.value.reason


def test_submit_accepts_contiguous_nonces_and_rejection_reasons_are_distinct():
    state, pool = make_pool()
    from conftest import show, explain

    inputs = [
        ("alice", "bob", 0, 30, 5, "a0"),
        ("alice", "bob", 0, 30, 5, "a0"),       # 完全重复
        ("alice", "carol", 0, 1, 1, "a0-other"),  # 同序号槽位另一笔
        ("alice", "bob", 5, 1, 0, "a5"),         # 序号断层
        ("ghost", "bob", 0, 1, 0, "g0"),         # 未知发送方
        ("alice", "bob", 1, 66, 0, "a1-poor"),   # 累计余额不足
        ("bob", "alice", 0, 50, 0, "b0"),
        ("bob", "alice", 0, 1, 1, "b0-dup-id"),  # bob 的 0 槽被 b0 占
    ]
    show("提交输入（顺序即并发到达顺序的串行化）", inputs)

    results = {}
    pool.submit(tx(*inputs[0]))
    results["duplicate"] = reject_reason(lambda: pool.submit(tx(*inputs[1])))
    results["conflict"] = reject_reason(lambda: pool.submit(tx(*inputs[2])))
    results["gap"] = reject_reason(lambda: pool.submit(tx(*inputs[3])))
    results["unknown"] = reject_reason(lambda: pool.submit(tx(*inputs[4])))
    results["balance"] = reject_reason(lambda: pool.submit(tx(*inputs[5])))
    pool.submit(tx(*inputs[6]))
    results["occupied_other"] = reject_reason(lambda: pool.submit(tx(*inputs[7])))

    explain("各类拒绝原因", results)
    assert results == {
        "duplicate": RejectReason.DUPLICATE_TX,
        "conflict": RejectReason.NONCE_CONFLICT,
        "gap": RejectReason.NONCE_GAP,
        "unknown": RejectReason.UNKNOWN_SENDER,
        "balance": RejectReason.INSUFFICIENT_BALANCE,
        "occupied_other": RejectReason.NONCE_CONFLICT,
    }

    # 被拒绝的交易没有顶掉池里已有的 a0 / b0
    assert pool.pending[("alice", 0)].tx_id == "a0"
    assert pool.pending[("bob", 0)].tx_id == "b0"
    explain("拒绝后池内容（未被覆盖）", {
        "slots": sorted(pool.pending.keys()),
        "tx_ids": [t.tx_id for t in sorted(pool.pending.values(), key=lambda x: x.tx_id)],
    })


def test_rejected_submit_does_not_mutate_pool():
    _, pool = make_pool()
    pool.submit(tx("alice", "bob", 0, 10, 1, "ok1"))
    before = dict(pool.pending)
    for bad in [
        tx("alice", "bob", 9, 1, 0, "x-gap"),
        tx("alice", "bob", 1, 1_000_000, 0, "x-poor"),
        tx("ghost", "bob", 0, 1, 0, "x-unknown"),
    ]:
        with pytest.raises(LedgerError):
            pool.submit(bad)
        assert pool.pending == before
    assert len(pool.pending) == 1


def test_candidate_order_is_fee_desc_then_tx_id_asc():
    _, pool = make_pool()
    # alice: 100；每笔 cost 不超过 20，连排 5 笔
    for i, (fee, txid) in enumerate([
        (1, "z-low"), (5, "m-mid"), (5, "a-mid"), (9, "hi"), (0, "zero")
    ]):
        pool.submit(tx("alice", "bob", i, 10, fee, txid))
    order = [t.tx_id for t in pool.ordered_candidates()]
    assert order == ["hi", "a-mid", "m-mid", "z-low", "zero"]


def test_select_for_block_skips_blocked_and_keeps_them_pending():
    state, pool = make_pool()
    # 两笔都合法入池；但 a1 手续费更高，全局序排在 a0 前面。
    # 扫描先遇 a1 -> 序号断层跳过；之后 a0、b0 收录；a1 必须留到下一块。
    pool.submit(tx("alice", "bob", 0, 10, 1, "a0-lowfee"))
    pool.submit(tx("alice", "carol", 1, 5, 9, "a1-hifee"))
    pool.submit(tx("bob", "alice", 0, 40, 2, "b0"))
    chosen = pool.select_for_block("miner")
    chosen_ids = [t.tx_id for t in chosen]
    # 全局序：a1-hifee(9) -> b0(2) -> a0-lowfee(1)
    # a1 扫描时断层 -> 跳过；b0、a0 收录；a1 留池
    assert chosen_ids == ["b0", "a0-lowfee"]
    assert ("alice", 1) in pool.pending

    # 应用选中交易后再选一次：a1 现在可执行
    for t in chosen:
        state.apply_tx(t, "miner")
    pool.remove_included(chosen)
    chosen2 = pool.select_for_block("miner")
    assert [t.tx_id for t in chosen2] == ["a1-hifee"]


def test_selection_is_deterministic_across_insertion_orders():
    submissions = [
        ("alice", "bob", 0, 10, 5, "a0"),
        ("alice", "bob", 1, 10, 5, "a1"),
        ("bob", "alice", 0, 10, 5, "b0"),
        ("carol", "alice", 0, 5, 5, "c0"),
    ]
    selections = []
    # 两种提交顺序都保证每个账户序号递增（否则提交时即 NONCE_GAP）
    alt_order = [submissions[2], submissions[0], submissions[3], submissions[1]]
    for order in [submissions, alt_order]:
        state = WorldState.genesis({"alice": 100, "bob": 50, "carol": 10})
        pool = Mempool(state=state)
        for s in order:
            pool.submit(tx(*s))
        selections.append([t.tx_id for t in pool.select_for_block("miner")])
    assert selections[0] == selections[1]
