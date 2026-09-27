"""分叉：链头切换、状态回放一致性、被切走分支找回、重复块不重复计入。"""
from __future__ import annotations

import threading

import pytest

from conftest import show_tx
from node.blockchain import BlockApplyPolicy, Chain
from node.errors import BlockRejected
from node.models import Block, Transaction
from node.node import Node

GENESIS = {"alice": 1000, "bob": 100}
TOTAL = 1100


def make_fork(chain: Chain, parent: str, height: int, sender: str,
              recipient: str, amount: int, fee: int, producer: str) -> tuple[object, Transaction]:
    state = chain.state_at(parent)
    tx = Transaction(sender, recipient, state.get(sender).nonce, amount, fee)
    block = Block(
        height=height,
        parent_id=parent,
        producer=producer,
        txs=(tx,),
    )
    show_tx(f"构造高度{height}区块内交易", tx)
    return block, tx


def test_head_switches_to_heavier_branch_and_state_replays() -> None:
    chain = Chain(GENESIS)
    g = chain.genesis_id()

    # 分支 A: g -> A1 -> A2（alice 向 bob 连续转账），沿链头延伸，每次都切换
    a1, _ = make_fork(chain, g, 1, "alice", "bob", 100, 1, "minerA")
    assert chain.add_block(a1).switched_head is True
    a2, _ = make_fork(chain, a1.block_id, 2, "alice", "bob", 100, 1, "minerA")
    assert chain.add_block(a2).switched_head is True  # 当前链头权重 3

    # 分支 B 从创世分叉
    b1, _ = make_fork(chain, g, 1, "bob", "alice", 10, 1, "minerB")
    r_b1 = chain.add_block(b1)
    print(f"B1(权重2) 接入, 当前链头权重=3 -> 切换={r_b1.switched_head}")
    assert r_b1.switched_head is False  # 当前头是 A2(权重3)，B1 权重 2
    b2, _ = make_fork(chain, b1.block_id, 2, "bob", "alice", 20, 1, "minerB")
    r_b2 = chain.add_block(b2)
    print(f"B2(权重3) 接入 -> 切换={r_b2.switched_head}（权重相等，先到先得不切换）")
    assert r_b2.switched_head is False
    b3, _ = make_fork(chain, b2.block_id, 3, "bob", "alice", 30, 1, "minerB")
    r_b3 = chain.add_block(b3)
    print(f"B3(权重4) 接入 -> 切换={r_b3.switched_head}")
    assert r_b3.switched_head is True
    assert chain.head_id == b3.block_id

    # 链头状态 = 沿新链从创世重放的状态
    snap = chain.state_at(b3.block_id)
    replay = chain.replay_state(b3.block_id, BlockApplyPolicy.REJECT_WHOLE_BLOCK)
    for name in ("alice", "bob", "minerA", "minerB"):
        x, y = snap.get(name), replay.get(name)
        print(f"账户 {name}: 快照余额={x.balance} nonce={x.nonce} | "
              f"重放余额={y.balance} nonce={y.nonce}")
        assert (x.balance, x.nonce) == (y.balance, y.nonce)
    # bob: 100 -10-1 -20-1 -30-1 = 36；alice: 1000+60=1060；minerB 收 3 笔 fee=3
    assert snap.get("bob").balance == 37
    assert snap.get("alice").balance == 1060
    assert snap.get("minerB").balance == 3
    assert snap.total_supply() == TOTAL


def test_abandoned_branch_state_is_recoverable() -> None:
    chain = Chain(GENESIS)
    g = chain.genesis_id()
    a1, _ = make_fork(chain, g, 1, "alice", "bob", 100, 1, "minerA")
    chain.add_block(a1)
    a2, _ = make_fork(chain, a1.block_id, 2, "alice", "bob", 100, 1, "minerA")
    chain.add_block(a2)
    b1, _ = make_fork(chain, g, 1, "bob", "alice", 10, 1, "minerB")
    chain.add_block(b1)
    b2, _ = make_fork(chain, b1.block_id, 2, "bob", "alice", 10, 1, "minerB")
    chain.add_block(b2)
    b3, _ = make_fork(chain, b2.block_id, 3, "bob", "alice", 10, 1, "minerB")
    chain.add_block(b3)
    assert chain.head_id == b3.block_id

    # A 分支虽被切走，状态仍可找回，且与独立重放一致
    old = chain.state_at(a2.block_id)
    replay = chain.replay_state(a2.block_id, BlockApplyPolicy.REJECT_WHOLE_BLOCK)
    print(f"被切走分支 A2: alice={old.get('alice').balance} "
          f"bob={old.get('bob').balance} minerA={old.get('minerA').balance}")
    assert old.get("alice").balance == 1000 - 200 - 2
    assert old.get("minerA").balance == 2
    assert old.total_supply() == TOTAL
    for name in ("alice", "bob", "minerA"):
        assert old.get(name) == replay.get(name)


def test_duplicate_block_delivered_twice_not_double_counted() -> None:
    chain = Chain(GENESIS)
    g = chain.genesis_id()
    a1, tx = make_fork(chain, g, 1, "alice", "bob", 100, 1, "minerA")
    chain.add_block(a1)
    miner_balance_after_first = chain.head_state().get("minerA").balance
    print(f"第一次送达后 minerA={miner_balance_after_first}")
    with pytest.raises(BlockRejected) as exc:
        chain.add_block(a1)
    print(f"第二次送达判定: {exc.value.reason.value} - {exc.value.detail}")
    assert exc.value.reason.value == "DUPLICATE_BLOCK"
    st = chain.head_state()
    assert st.get("minerA").balance == miner_balance_after_first  # 没有算两次
    assert st.get("alice").nonce == 1


def test_equal_weight_keeps_first_seen_head() -> None:
    chain = Chain(GENESIS)
    g = chain.genesis_id()
    a1, _ = make_fork(chain, g, 1, "alice", "bob", 100, 1, "minerA")
    chain.add_block(a1)
    b1, _ = make_fork(chain, g, 1, "bob", "alice", 10, 1, "minerB")
    result = chain.add_block(b1)
    print(f"等权重(都为2)分叉: switched={result.switched_head}, 链头保持 A1")
    assert result.switched_head is False
    assert chain.head_id == a1.block_id


def test_node_mempool_on_switch_oldbranch_tx_remined() -> None:
    """节点层：旧分支上打包、新链上未落地的交易应回到池中被重新打包，且只计入一次。"""
    node = Node(GENESIS, policy=BlockApplyPolicy.REJECT_WHOLE_BLOCK)
    g = node.chain.genesis_id()

    # 分支 A：alice 连续两笔上链（A1、A2），链头权重=3
    tx = Transaction("alice", "bob", 0, 100, 1)
    tx2 = Transaction("alice", "bob", 1, 100, 1)
    node.submit_tx(tx)
    node.produce_block("minerA")
    node.submit_tx(tx2)
    node.produce_block("minerA")
    assert node.pending_count() == 0

    # 从创世构造更长的 B 分支：不包含这两笔（bob->alice 三笔，金额各不相同
    # 以保证内容寻址下三块的 block_id 不同）
    def ext(parent_id: str, sender: str, amount: int, fee: int,
            producer: str, height: int) -> Block:
        st = node.chain.state_at(parent_id)
        recipient = "alice" if sender == "bob" else "bob"
        t = Transaction(sender, recipient, st.get(sender).nonce, amount, fee)
        parent_block = node.chain.path_from_genesis(parent_id)[-1]
        return Block(
            height=height,
            parent_id=parent_id,
            producer=producer,
            txs=(t,),
        )

    b1 = ext(g, "bob", 10, 1, "minerB", 1)
    assert node.add_block(b1).switched_head is False  # 权重 2 < 3
    b2 = ext(b1.block_id, "bob", 20, 1, "minerB", 2)
    assert node.add_block(b2).switched_head is False  # 权重相等，先到先得
    b3 = ext(b2.block_id, "bob", 30, 1, "minerB", 3)
    switched = node.add_block(b3)                     # 权重 4 > 3，切换
    print(f"切换到 B3: {switched.switched_head}; 旧分支两笔 alice 交易回到池中")
    assert switched.switched_head is True
    assert node.nonce_of("alice") == 0
    assert node.pending_count() == 2
    # 重新出块：两笔旧交易按原 nonce 顺序被再打包，但状态中只计入一次
    reblock = node.produce_block("minerA")
    ids = [t.tx_id for t in reblock.txs]
    assert ids == [tx.tx_id, tx2.tx_id]
    assert node.nonce_of("alice") == 2
    assert node.total_supply() == TOTAL
    # alice 只被扣了一次：1000 - 200 - 2(fee) + 60(bob 转来) = 858
    assert node.balance_of("alice") == 858
    print(f"重打包成功: 总量={node.total_supply()}, alice={node.balance_of('alice')}")


def test_concurrent_fork_extensions_remain_consistent() -> None:
    """两个线程并发往同一条链上接块，最终链状态与任一线性化重放一致且总量守恒。"""
    chain = Chain(GENESIS)
    g = chain.genesis_id()
    errors: list[BaseException] = []
    lock = threading.Lock()

    def extend(parent_start: str, sender: str, recipient: str, producer: str,
                count: int) -> None:
        parent = parent_start
        try:
            for i in range(count):
                st = chain.state_at(parent)
                # 金额随 i 变化，保证每个块内容（因而 block_id）不同
                tx = Transaction(sender, recipient, st.get(sender).nonce, i + 1, 0)
                parent_block = chain.path_from_genesis(parent)[-1]
                block = Block(
                    height=parent_block.height + 1,
                    parent_id=parent,
                    producer=producer,
                    txs=(tx,),
                )
                chain.add_block(block)
                parent = block.block_id
        except BaseException as exc:  # 并发下可能形成等权重/更低权重分支，均记录
            with lock:
                errors.append(exc)

    t1 = threading.Thread(target=extend, args=(g, "alice", "bob", "pA", 5))
    t2 = threading.Thread(target=extend, args=(g, "bob", "alice", "pB", 5))
    t1.start(); t2.start(); t1.join(); t2.join()

    head = chain.head
    print(f"并发接块后链头: 高度={head.height}, id={head.block_id[:8]}, "
          f"异常数={len(errors)}（允许的非切换情形）")
    assert head.height == 5  # 两条等长竞争，先到者保持链头
    snap = chain.state_at(head.block_id)
    replay = chain.replay_state(head.block_id, BlockApplyPolicy.REJECT_WHOLE_BLOCK)
    assert snap.total_supply() == TOTAL
    for name in ("alice", "bob"):
        assert snap.get(name).balance == replay.get(name).balance
        assert snap.get(name).nonce == replay.get(name).nonce
    print(f"快照==重放, 总量={snap.total_supply()}")
