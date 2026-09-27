"""出块：选交易规则、余额与总量守恒、非法交易的两种处理策略。"""
from __future__ import annotations

import pytest

from conftest import show_tx
from node.blockchain import BlockApplyPolicy, Chain
from node.errors import BlockRejected
from node.models import Block, Transaction
from node.node import Node

GENESIS = {"alice": 1000, "bob": 100, "miner": 0}
TOTAL = 1100


def test_block_selection_rule_fee_desc_then_id_asc() -> None:
    """规则：逐轮按 (fee 降序, tx_id 升序) 扫描，把本轮变为可执行的交易按扫描顺序
    追加进块；同账户必须按 nonce 连续，因此高 fee 的 t1 只能等 t0 先入块。
    入块顺序 = 追加顺序，保证块按此顺序应用时每笔都合法。
    """
    node = Node(GENESIS)
    t0 = Transaction("alice", "bob", 0, 50, 1)
    t1 = Transaction("alice", "bob", 1, 50, 10)  # 更高手续费，但依赖 t0 先入块
    t_low = Transaction("bob", "alice", 0, 10, 0)
    for t in (t0, t1, t_low):
        node.submit_tx(t)
        show_tx("入池", t)
    block = node.produce_block("miner")
    ids = [t.tx_id for t in block.txs]
    print("入块顺序:", [t.tx_id[:8] for t in block.txs], "各笔 fee:", [t.fee for t in block.txs])
    # 第 1 轮: t1(fee10) nonce 断层跳过 -> t0(fee1) 追加 -> t_low(fee0) 追加
    # 第 2 轮: t1(fee10) 变为可执行，追加
    assert ids == [t0.tx_id, t_low.tx_id, t1.tx_id]
    # 同账户 nonce 顺序必然满足
    assert ids.index(t0.tx_id) < ids.index(t1.tx_id)


def test_balances_and_total_supply_after_block() -> None:
    node = Node(GENESIS)
    t0 = Transaction("alice", "bob", 0, 200, 7)
    t1 = Transaction("bob", "alice", 0, 30, 2)
    node.submit_tx(t0)
    node.submit_tx(t1)
    block = node.produce_block("miner")
    print(f"块高 {block.height}, 打包 {len(block.txs)} 笔")
    # alice: 1000 - 200 - 7 + 30 = 823
    # bob:   100 + 200 - 30 - 2 = 268
    # miner: 0 + 7 + 2 = 9
    assert node.balance_of("alice") == 823
    assert node.balance_of("bob") == 268
    assert node.balance_of("miner") == 9
    assert node.total_supply() == TOTAL
    print(f"余额 alice=823 bob=268 miner=9, 总量={node.total_supply()}")
    # 手续费和 nonce 已落地，池中交易被清空
    assert node.pending_count() == 0
    assert node.nonce_of("alice") == 1
    assert node.nonce_of("bob") == 1


def test_empty_block_still_advances_chain() -> None:
    node = Node(GENESIS)
    block = node.produce_block("miner")
    assert block.height == 1
    assert block.txs == ()
    assert node.total_supply() == TOTAL
    print(f"空块: 高度={block.height}, 总量={node.total_supply()} 不变")


def test_invalid_tx_in_block_rejects_whole_block() -> None:
    """严格策略：块里有一笔非法 -> 整块拒绝，状态一笔都不变。"""
    chain = Chain(GENESIS)
    good = Transaction("alice", "bob", 0, 100, 1)
    bad = Transaction("alice", "bob", 2, 100, 1)  # nonce 断层
    block = Block(height=1, parent_id=chain.genesis_id(), producer="evil",
                  txs=(good, bad))
    show_tx("块内合法交易", good)
    show_tx("块内非法交易(nonce 断层)", bad)
    with pytest.raises(BlockRejected) as exc:
        chain.add_block(block, BlockApplyPolicy.REJECT_WHOLE_BLOCK)
    print(f"判定依据: {exc.value.reason.value} - {exc.value.detail}")
    assert exc.value.reason.value == "INVALID_BLOCK"
    # 整块都没进链：高度仍停在创世，余额没变，重复送仍然报同一类错
    assert chain.head_id == chain.genesis_id()
    st = chain.head_state()
    assert st.get("alice").balance == 1000
    assert st.get("alice").nonce == 0
    with pytest.raises(BlockRejected) as exc2:
        chain.add_block(block, BlockApplyPolicy.REJECT_WHOLE_BLOCK)
    print(f"再次送达同一非法块: {exc2.value.reason.value}")
    assert exc2.value.reason.value == "INVALID_BLOCK"


def test_skip_policy_records_each_skipped_tx() -> None:
    """剔除策略：非法单笔被剔除并记录 (tx_id, 原因)，其余正常应用，总量守恒。"""
    chain = Chain(GENESIS)
    good = Transaction("alice", "bob", 0, 100, 1)
    bad_nonce = Transaction("alice", "bob", 2, 100, 1)       # 断层
    bad_funds = Transaction("bob", "alice", 0, 9999, 1)       # 余额不足
    bad_field = Transaction("alice", "bob", 0, 0, 1)          # 金额为 0
    block = Block(height=1, parent_id=chain.genesis_id(), producer="miner",
                  txs=(good, bad_nonce, bad_funds, bad_field))
    for tx in block.txs:
        show_tx("块内交易", tx)
    result = chain.add_block(block, BlockApplyPolicy.SKIP_INVALID_TX)
    print(f"判定: 整块接入，被剔除 {len(result.skipped)} 笔: {result.skipped}")
    assert len(result.skipped) == 3
    reasons = dict(result.skipped)
    assert reasons[bad_nonce.tx_id] == "NONCE_GAP"
    assert reasons[bad_funds.tx_id] == "INSUFFICIENT_FUNDS"
    assert reasons[bad_field.tx_id] == "INVALID_TRANSACTION"
    # 只有 good 生效：alice 1000-100-1=899, bob 100+100=200, miner=1
    st = chain.head_state()
    assert st.get("alice").balance == 899
    assert st.get("alice").nonce == 1
    assert st.get("bob").balance == 200
    assert st.get("miner").balance == 1
    assert st.total_supply() == TOTAL
    # 同一区块再次送达：DUPLICATE_BLOCK，绝不重复计入
    with pytest.raises(BlockRejected) as exc:
        chain.add_block(block, BlockApplyPolicy.SKIP_INVALID_TX)
    print(f"重复送块判定: {exc.value.reason.value} - {exc.value.detail}")
    assert exc.value.reason.value == "DUPLICATE_BLOCK"
    assert chain.head_state().get("miner").balance == 1  # 手续费没有算两次


def test_unknown_parent_and_bad_height_rejected() -> None:
    chain = Chain(GENESIS)
    orphan = Block(height=1, parent_id="deadbeef" * 8, producer="x", txs=())
    with pytest.raises(BlockRejected) as exc:
        chain.add_block(orphan)
    print(f"孤儿块判定: {exc.value.reason.value}")
    assert exc.value.reason.value == "UNKNOWN_PARENT"
    bad_h = Block(height=9, parent_id=chain.genesis_id(), producer="x", txs=())
    with pytest.raises(BlockRejected) as exc:
        chain.add_block(bad_h)
    print(f"高度不连续判定: {exc.value.reason.value}")
    assert exc.value.reason.value == "INVALID_HEIGHT"
