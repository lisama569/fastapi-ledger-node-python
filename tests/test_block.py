"""出块 / 收块（strict 与 skip 两种策略）与总量守恒测试。"""
from __future__ import annotations

import pytest

from node.errors import LedgerError, RejectReason
from node.models import Transaction, build_block
from node.node import LedgerNode


def tx(s, r, n, a, f, i):
    return Transaction(s, r, n, a, f, i)


def block_payload(height, parent, producer, txs, weight=1):
    b = build_block(
        height=height, parent_id=parent, producer=producer, transactions=txs,
        weight=weight,
    )
    return {
        "height": b.height,
        "parent_id": b.parent_id,
        "producer": b.producer,
        "weight": b.weight,
        "transactions": [t.to_canonical_dict() for t in b.transactions],
    }, b


def test_produced_block_picks_by_fee_rule_and_advances_state():
    node = LedgerNode({"alice": 100, "bob": 50})
    node.submit_tx({"sender": "alice", "recipient": "bob", "nonce": 0,
                    "amount": 10, "fee": 2, "tx_id": "a0"})
    node.submit_tx({"sender": "bob", "recipient": "alice", "nonce": 0,
                    "amount": 20, "fee": 8, "tx_id": "b0"})
    out = node.produce_block("miner")
    print("\n[输入] 池中 a0(fee=2), b0(fee=8)")
    print(f"[判定] 出块收录顺序={out['included_tx_ids']}（手续费高者先）")
    assert out["included_tx_ids"] == ["b0", "a0"]
    st = node.chain.tip_state
    # a0: alice -12, bob +10, miner +2
    # b0: bob -28, alice +20, miner +8
    assert st.get_balance("alice") == 100 - 12 + 20
    assert st.get_balance("bob") == 50 + 10 - 28
    assert st.get_balance("miner") == 10
    assert st.get_nonce("alice") == 1
    assert st.get_nonce("bob") == 1
    assert node.total_supply() == 150
    assert node.state_check()["replay_matches_snapshot"] is True


def test_strict_block_with_one_illegal_tx_is_rejected_whole_and_nothing_changes():
    node = LedgerNode({"alice": 100})
    before = node.chain.tip_state.snapshot()
    payload, bad_block = block_payload(
        height=1, parent="GENESIS", producer="miner",
        txs=[
            tx("alice", "bob", 0, 10, 1, "good"),
            tx("alice", "bob", 5, 1, 0, "gap-later"),  # 块内第二笔才非法
        ],
    )
    res = node.receive_block(payload, policy="strict")
    print("\n[输入] strict 块：good 合法在前，gap-layer 序号断层在后")
    print(f"[判定] status={res['status']} reason={res['reject_reason']} "
          f"detail={res['detail']}")
    assert res["status"] == "rejected"
    assert res["reject_reason"] == RejectReason.BLOCK_HAS_INVALID_TX.value
    # 第一笔也不能被扣（绝不半扣）
    assert node.chain.tip_state.accounts == before.accounts
    # 整块不存储
    assert node.chain.get_block(bad_block.block_id) is None
    assert node.tip()["block_id"] == "GENESIS"


def test_skip_block_excludes_only_illegal_tx_and_applies_rest_atomically():
    node = LedgerNode({"alice": 100})
    before = node.chain.tip_state.snapshot()
    payload, block = block_payload(
        height=1, parent="GENESIS", producer="miner",
        txs=[
            tx("alice", "bob", 0, 10, 1, "good"),
            tx("alice", "bob", 1, 1_000_000, 0, "poor"),   # 单笔剔除
            tx("alice", "bob", 2, 5, 2, "also-good"),      # poor 剔除后此笔变断层也剔除
        ],
    )
    res = node.receive_block(payload, policy="skip")
    excluded = [(e["tx_id"], e["reason"]) for e in res["excluded"]]
    print("\n[输入] skip 块：good / poor(余额不足) / also-good(随后断层)")
    print(f"[判定] 收录={res['included_tx_ids']} 剔除={excluded}")
    assert res["status"] == "accepted"
    assert res["included_tx_ids"] == ["good"]
    assert dict(excluded)["poor"] == RejectReason.INSUFFICIENT_BALANCE.value
    assert dict(excluded)["also-good"] == RejectReason.NONCE_GAP.value
    assert node.chain.get_block(block.block_id) is not None
    st = node.chain.tip_state
    assert st.get_balance("alice") == 100 - 11
    assert st.get_balance("bob") == 10
    assert st.get_balance("miner") == 1
    assert st.get_nonce("alice") == 1
    assert node.total_supply() == 100
    assert node.state_check()["replay_matches_snapshot"] is True


def test_replaying_skipped_block_again_does_not_double_count():
    node = LedgerNode({"alice": 100})
    payload, block = block_payload(
        height=1, parent="GENESIS", producer="miner",
        txs=[tx("alice", "bob", 0, 10, 1, "good"),
             tx("alice", "bob", 9, 1, 0, "bad")],
    )
    node.receive_block(payload, policy="skip")
    s1 = node.chain.state_at(block.block_id).snapshot()
    # 手工触发多次"从创世重放"，结果必须始终等于缓存快照
    for _ in range(3):
        assert node.chain.replay_state(block.block_id).accounts == s1.accounts
    assert node.chain.check_replay_consistency(block.block_id)
    # 缓存里的真实状态也只算了 good 一次
    assert node.chain.state_at(block.block_id).get_nonce("alice") == 1
    print("\n[判定] skip 块多次重放结果一致，非法交易从未计入，alice nonce=1")


def test_duplicate_inblock_and_already_applied_are_classified():
    node = LedgerNode({"alice": 100})
    payload1, b1 = block_payload(
        1, "GENESIS", "m", [tx("alice", "bob", 0, 10, 1, "g1")]
    )
    node.receive_block(payload1, "strict")

    # 同一个块（同内容 -> 同 block_id）再送一次：幂等，不重复计入
    dup = node.receive_block(payload1, "strict")
    print(f"\n[判定] 重复送块: status={dup['status']} reason={dup['reject_reason']}")
    assert dup["status"] == "duplicate"
    assert dup["reject_reason"] == RejectReason.DUPLICATE_BLOCK.value
    assert node.chain.state_at(b1.block_id).get_balance("m") == 1
    assert node.chain.state_at(b1.block_id).get_nonce("alice") == 1

    # 在子块里把已生效的 g1 再塞进去：strict 先判 -> 整块拒绝且不存储
    payload2, b2 = block_payload(
        2, b1.block_id, "m", [tx("alice", "bob", 0, 10, 1, "g1")]
    )
    strict = node.receive_block(payload2, "strict")
    assert strict["status"] == "rejected"
    assert strict["reject_reason"] == RejectReason.BLOCK_HAS_INVALID_TX.value
    assert node.chain.get_block(b2.block_id) is None
    # 同样内容改走 skip：单笔剔除（空块），可以存
    skip = node.receive_block(payload2, "skip")
    assert dict((e["tx_id"], e["reason"]) for e in skip["excluded"]) == {
        "g1": RejectReason.ALREADY_APPLIED.value
    }
    print("[判定] 已生效交易在子块: strict->整块拒绝不存储; skip->already_applied 单笔剔除")


def test_tampered_hash_and_unknown_parent_rejected_with_distinct_reasons():
    node = LedgerNode({"alice": 100})
    payload, _ = block_payload(
        1, "GENESIS", "m", [tx("alice", "bob", 0, 10, 1, "g1")]
    )
    # 携带原 block_id 但内容被改 -> 重算摘要对不上
    tampered = dict(payload)
    tampered["block_id"] = node.chain.blocks and __import__(
        "node.models", fromlist=["block_digest"]
    ).block_digest(
        __import__("node.models", fromlist=["build_block"]).build_block(
            height=1, parent_id="GENESIS", producer="m",
            transactions=[tx("alice", "bob", 0, 10, 1, "g1")],
        )
    )
    tampered["producer"] = "attacker"
    with pytest.raises(LedgerError) as e1:
        node.receive_block(tampered, "strict")
    assert e1.value.reason == RejectReason.BAD_BLOCK_HASH

    orphan, _ = block_payload(
        3, "MISSING", "m", [tx("alice", "bob", 0, 10, 1, "g1")]
    )
    with pytest.raises(LedgerError) as e2:
        node.receive_block(orphan, "strict")
    assert e2.value.reason == RejectReason.UNKNOWN_PARENT
    print("\n[判定] 篡改块->bad_block_hash；孤儿块->unknown_parent，原因可区分")
