"""FastAPI 入口：把本地简化链节点的能力暴露为 HTTP 接口（全部内存运行）。"""
from __future__ import annotations

from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from node.blockchain import BlockApplyPolicy
from node.errors import BlockRejected, TxRejected
from node.models import Block, Transaction
from node.node import Node

app = FastAPI(title="local-ledger-node", version="0.1.0")

# 单节点内存实例；可通过 /admin/reset 重新创世。
_node = Node({"alice": 1000, "bob": 100}, BlockApplyPolicy.REJECT_WHOLE_BLOCK)


def get_node() -> Node:
    return _node


class TxIn(BaseModel):
    sender: str
    recipient: str
    nonce: int
    amount: int
    fee: int


class ResetIn(BaseModel):
    genesis_alloc: dict[str, int] = Field(default_factory=dict)
    policy: Literal["REJECT_WHOLE_BLOCK", "SKIP_INVALID_TX"] = "REJECT_WHOLE_BLOCK"
    max_block_txs: int = 100


class ProduceIn(BaseModel):
    producer: str


class BlockIn(BaseModel):
    height: int
    parent_id: str
    producer: str
    txs: list[TxIn] = Field(default_factory=list)


def _tx_to_json(tx: Transaction) -> dict:
    return {
        "tx_id": tx.tx_id,
        "sender": tx.sender,
        "recipient": tx.recipient,
        "nonce": tx.nonce,
        "amount": tx.amount,
        "fee": tx.fee,
    }


def _block_to_json(block: Block) -> dict:
    return {
        "block_id": block.block_id,
        "height": block.height,
        "parent_id": block.parent_id,
        "producer": block.producer,
        "txs": [_tx_to_json(t) for t in block.txs],
    }


def _state_to_json(block_id: str, state) -> dict:
    return {
        "block_id": block_id,
        "accounts": {
            name: {"balance": a.balance, "nonce": a.nonce}
            for name, a in state._accounts.items()  # noqa: SLF001
        },
        "total_supply": state.total_supply(),
    }


@app.get("/")
def read_root() -> dict:
    return {"service": "local-ledger-node", "head": get_node().chain.head_id}


@app.post("/admin/reset")
def reset(body: ResetIn) -> dict:
    """丢弃全部内存数据，按新创世分配重建节点。"""
    global _node
    _node = Node(
        body.genesis_alloc,
        BlockApplyPolicy(body.policy),
        body.max_block_txs,
    )
    return {"ok": True, "genesis_id": _node.chain.genesis_id()}


@app.get("/state")
def state() -> dict:
    """当前链头状态：各账户余额/nonce、总量、待处理交易数。"""
    node = get_node()
    data = _state_to_json(node.chain.head_id, node.chain.head_state())
    data["pending"] = node.pending_count()
    return data


@app.post("/txs", status_code=201)
def submit_tx(body: TxIn) -> dict:
    """提交交易入池。非法输入返回 409，body 中 reason 可区分原因。"""
    node = get_node()
    tx = Transaction(body.sender, body.recipient, body.nonce, body.amount, body.fee)
    try:
        tx_id = node.submit_tx(tx)
    except TxRejected as exc:
        raise HTTPException(status_code=409, detail={"reason": exc.reason.value,
                                                     "message": exc.detail}) from exc
    return {"tx_id": tx_id}


@app.post("/blocks/produce", status_code=201)
def produce_block(body: ProduceIn) -> dict:
    """本节点按稳定规则选交易出块并接在链头。"""
    return _block_to_json(get_node().produce_block(body.producer))


@app.post("/blocks", status_code=201)
def add_block(body: BlockIn) -> dict:
    """接收外部区块；返回是否切换链头及被剔除交易（SKIP 策略）。"""
    node = get_node()
    block = Block(
        height=body.height,
        parent_id=body.parent_id,
        producer=body.producer,
        txs=tuple(
            Transaction(t.sender, t.recipient, t.nonce, t.amount, t.fee)
            for t in body.txs
        ),
    )
    try:
        result = node.add_block(block)
    except BlockRejected as exc:
        raise HTTPException(status_code=409, detail={"reason": exc.reason.value,
                                                     "message": exc.detail}) from exc
    return {
        "block_id": result.block_id,
        "switched_head": result.switched_head,
        "skipped": [{"tx_id": t, "reason": r} for t, r in result.skipped],
    }


@app.get("/blocks/{block_id}/state")
def block_state(block_id: str) -> dict:
    """查看任意已入链区块之后的状态（被切走的分支也能找回）。"""
    node = get_node()
    if not node.chain.has_block(block_id):
        raise HTTPException(status_code=404, detail={"reason": "BLOCK_NOT_FOUND"})
    return _state_to_json(block_id, node.chain.state_at(block_id))


@app.get("/blocks/{block_id}/verify-replay")
def verify_replay(block_id: str) -> dict:
    """从创世独立重放该块路径，与入链时保存的快照逐账户比对。"""
    node = get_node()
    if not node.chain.has_block(block_id):
        raise HTTPException(status_code=404, detail={"reason": "BLOCK_NOT_FOUND"})
    snap = node.chain.state_at(block_id)
    replay = node.chain.replay_state(block_id, node._policy)  # noqa: SLF001
    matches = True
    diffs: list[dict] = []
    names = set(snap._accounts) | set(replay._accounts)  # noqa: SLF001
    for name in sorted(names):
        a, b = snap.get(name), replay.get(name)
        if (a.balance, a.nonce) != (b.balance, b.nonce):
            matches = False
            diffs.append({"account": name,
                          "snapshot": {"balance": a.balance, "nonce": a.nonce},
                          "replay": {"balance": b.balance, "nonce": b.nonce}})
    return {"matches": matches, "diffs": diffs}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
