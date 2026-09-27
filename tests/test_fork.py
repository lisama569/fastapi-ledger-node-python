"""分叉链头切换、重放一致性、重复块幂等、旧分支状态找回测试。"""
from __future__ import annotations

from node.models import Transaction, build_block
from node.node import LedgerNode


def T(s, r, n, a, f, i):
    return Transaction(s, r, n, a, f, i)


def B(height, parent, producer, txs, weight=1):
    return build_block(
        height=height, parent_id=parent, producer=producer,
        transactions=txs, weight=weight,
    )


def send(node, block: Transaction | object, policy="strict"):
    payload = {
        "height": block.height,
        "parent_id": block.parent_id,
        "producer": block.producer,
        "weight": block.weight,
        "transactions": [t.to_canonical_dict() for t in block.transactions],
    }
    return node.receive_block(payload, policy=policy)


def test_heavier_fork_switches_and_state_equals_fresh_replay():
    node = LedgerNode({"alice": 1000, "bob": 1000})

    # 链 A：GENESIS -> A1(w1) -> A2(w1)，累计权重 2
    a1 = build_block(height=1, parent_id="GENESIS", producer="A",
                     transactions=[T("alice", "bob", 0, 100, 10, "a-t1")], weight=1)
    a2 = build_block(height=2, parent_id=a1.block_id, producer="A",
                     transactions=[T("bob", "alice", 0, 200, 10, "a-t2")], weight=1)
    assert send(node, a1)["switched"] is True
    assert send(node, a2)["switched"] is True
    state_on_A = node.chain.tip_state.snapshot()

    # 链 B：只从 GENESIS 延伸一个高权重块（w5），累计 5 > 2
    b1 = build_block(height=1, parent_id="GENESIS", producer="B",
                     transactions=[T("alice", "carol", 0, 300, 30, "b-t1"),
                                   T("bob", "carol", 0, 400, 20, "b-t2")], weight=5)
    res = send(node, b1)
    print("\n[输入] 链A累计权重2，分叉链B单块权重5")
    print(f"[判定] 接收B: status={res['status']} switched={res['switched']} tip={res['tip_id'][:10]}")
    assert res["switched"] is True
    assert node.tip()["block_id"] == b1.block_id

    # 切换后状态 == 不使用缓存、从创世按新链逐笔重放
    fresh = node.chain.replay_state(b1.block_id)
    cached = node.chain.state_at(b1.block_id)
    assert fresh.accounts == cached.accounts
    print(f"[判定] B状态: alice={cached.get_balance('alice')} "
          f"bob={cached.get_balance('bob')} carol={cached.get_balance('carol')} "
          f"B={cached.get_balance('B')} 总量={cached.total_supply()}")
    assert cached.get_balance("alice") == 1000 - 330
    assert cached.get_balance("bob") == 1000 - 420
    assert cached.get_balance("carol") == 700
    assert cached.get_balance("B") == 50
    assert cached.total_supply() == 2000

    # 被切走的 A 分支：块和状态快照都还找得回来
    assert node.chain.get_block(a2.block_id) is not None
    recovered = node.chain.state_at(a2.block_id)
    assert recovered.accounts == state_on_A.accounts
    assert node.chain.check_replay_consistency(a2.block_id)
    print("[判定] 旧分支 A2 的状态仍可找回，且自身重放一致")


def test_equal_weight_does_not_switch_first_tip_wins():
    node = LedgerNode({"alice": 1000})
    a1 = B(1, "GENESIS", "A", [T("alice", "bob", 0, 10, 1, "x")], weight=3)
    b1 = B(1, "GENESIS", "B", [T("alice", "carol", 0, 10, 1, "y")], weight=3)
    send(node, a1)
    res = send(node, b1)
    print(f"\n[判定] 等重分叉: switched={res['switched']}（必须 False，先到为准）")
    assert res["switched"] is False
    assert node.tip()["block_id"] == a1.block_id
    # 再补一个权重 1 的块给 B -> 4 > 3 才切
    b2 = B(2, b1.block_id, "B", [T("alice", "carol", 1, 10, 1, "z")], weight=1)
    res2 = send(node, b2)
    assert res2["switched"] is True
    print("[判定] B 再延伸 w1 累计4 > 3，完成切换")


def test_switch_back_and_forth_states_always_match_replay():
    node = LedgerNode({"alice": 500, "bob": 500})
    a1 = B(1, "GENESIS", "A", [T("alice", "bob", 0, 50, 5, "a1")], 1)
    b1 = B(1, "GENESIS", "B", [T("bob", "alice", 0, 70, 7, "b1")], 2)
    b2 = B(2, b1.block_id, "B", [T("bob", "alice", 1, 70, 7, "b2")], 2)
    a2 = B(2, a1.block_id, "A", [T("alice", "bob", 1, 50, 5, "a2")], 4)

    send(node, a1)
    assert node.tip()["block_id"] == a1.block_id
    send(node, b1)  # B 累计 2 > A 1
    assert node.tip()["block_id"] == b1.block_id
    send(node, b2)  # B 累计 4
    assert node.tip()["block_id"] == b2.block_id
    res = send(node, a2)  # A 累计 5 > B 4，切回去
    assert res["switched"] is True
    assert node.tip()["block_id"] == a2.block_id

    for bid, name in [(a1.block_id, "A1"), (b1.block_id, "B1"),
                      (b2.block_id, "B2"), (a2.block_id, "A2")]:
        ok = node.chain.check_replay_consistency(bid)
        fresh = node.chain.replay_state(bid).accounts
        assert ok and fresh == node.chain.state_at(bid).accounts
    tip_fresh = node.chain.replay_state(node.tip()["block_id"]).accounts
    assert tip_fresh == node.chain.tip_state.accounts
    print("\n[判定] A->B->B->A 来回切换后，四条块的快照与全新重放全部一致")


def test_same_block_delivered_twice_is_counted_once_even_as_fork():
    node = LedgerNode({"alice": 100})
    b = B(1, "GENESIS", "M", [T("alice", "bob", 0, 30, 3, "t")], 1)
    send(node, b)
    alice_after_first = node.chain.state_at(b.block_id).get_balance("alice")

    dup = send(node, b)
    assert dup["status"] == "duplicate"
    assert dup["reject_reason"] == "duplicate_block"
    assert dup["switched"] is False
    assert node.chain.state_at(b.block_id).get_balance("alice") == alice_after_first
    assert node.chain.state_at(b.block_id).get_nonce("alice") == 1
    # 存储里只有 GENESIS + 这一个块，没有产生第二个条目
    assert len(node.chain.blocks) == 2
    print(f"\n[判定] 同一份块送两次: 第二次 duplicate_block，alice 余额={alice_after_first} 未再变")


def test_skip_fork_replay_after_switch_matches_and_excluded_tx_lost():
    node = LedgerNode({"alice": 100})
    # 主链 A 正常
    a1 = B(1, "GENESIS", "A", [T("alice", "bob", 0, 10, 1, "a-good")], 1)
    send(node, a1)
    # 分叉 B（skip 策略）：含一笔非法交易，只有合法的被收录
    b1 = B(1, "GENESIS", "B",
                     [T("alice", "carol", 0, 20, 2, "b-good"),
                      T("alice", "carol", 9, 20, 2, "b-bad")], 5)
    res = send(node, b1, policy="skip")
    assert res["included_tx_ids"] == ["b-good"]
    assert res["switched"] is True
    cached = node.chain.state_at(b1.block_id)
    fresh = node.chain.replay_state(b1.block_id)
    assert cached.accounts == fresh.accounts
    assert node.chain.check_replay_consistency(b1.block_id)
    assert cached.get_nonce("alice") == 1
    assert cached.get_balance("carol") == 20
    assert cached.get_balance("B") == 2
    print("\n[输入] 分叉B为skip块：b-good合法，b-bad序号断层")
    print("[判定] 切换到B后，缓存状态==从创世重放（非法交易从未计入，未双算）")
