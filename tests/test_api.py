"""HTTP API 冒烟测试。"""
from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from node.api import create_app


@pytest.fixture()
def client():
    return TestClient(create_app({"alice": 100, "bob": 50}))


def test_full_flow_over_http(client):
    print("\n[输入] GET / 与初始账户")
    assert client.get("/").json()["service"] == "local-ledger-node"
    accounts = client.get("/accounts").json()
    print(f"[判定] 初始总量={accounts['total_supply']} 账户数={len(accounts['accounts'])}")
    assert accounts["total_supply"] == 150

    r = client.post("/transactions", json={
        "sender": "alice", "recipient": "bob", "nonce": 0,
        "amount": 10, "fee": 5, "tx_id": "t1"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"

    # 重复提交：409 + duplicate_tx
    r = client.post("/transactions", json={
        "sender": "alice", "recipient": "bob", "nonce": 0,
        "amount": 10, "fee": 5, "tx_id": "t1"})
    assert r.status_code == 409 and r.json()["reason"] == "duplicate_tx"
    print(f"[判定] 重复提交 HTTP {r.status_code} reason={r.json()['reason']}")

    # 序号断层：409 + nonce_gap
    r = client.post("/transactions", json={
        "sender": "alice", "recipient": "bob", "nonce": 9,
        "amount": 1, "fee": 0, "tx_id": "gap"})
    assert r.status_code == 409 and r.json()["reason"] == "nonce_gap"

    # 坏输入：400 + bad_input
    r = client.post("/transactions", json={"sender": "alice"})
    assert r.status_code == 400 and r.json()["reason"] == "bad_input"

    r = client.post("/blocks/produce", json={"producer": "miner"})
    body = r.json()
    assert r.status_code == 200 and body["included_tx_ids"] == ["t1"]
    print(f"[判定] 出块 {body['block_id'][:10]} included={body['included_tx_ids']}")

    check = client.get("/debug/state-check").json()
    assert check["supply_conserved"] and check["replay_matches_snapshot"]
    print(f"[判定] 状态自检 {check}")

    # 更重分叉切过来
    r = client.post("/blocks/receive", json={
        "policy": "skip",
        "block": {
            "height": 1, "parent_id": "GENESIS", "producer": "rival", "weight": 9,
            "transactions": [
                {"sender": "alice", "recipient": "carol", "nonce": 0,
                 "amount": 80, "fee": 9, "tx_id": "r1"},
                {"sender": "alice", "recipient": "carol", "nonce": 9,
                 "amount": 1, "fee": 0, "tx_id": "r-bad"},
            ],
        }})
    body = r.json()
    assert body["status"] == "accepted" and body["switched"] is True
    assert body["included_tx_ids"] == ["r1"]
    assert body["excluded"][0]["reason"] == "nonce_gap"
    print(f"[判定] skip 分叉切换 included={body['included_tx_ids']} "
          f"excluded={[e['reason'] for e in body['excluded']]}")

    tip = client.get("/chain/tip").json()
    assert tip["cumulative_weight"] == 9
    blk = client.get(f"/blocks/{body['block_id']}").json()
    assert blk["result"]["included"][0]["tx_id"] == "r1"
    assert client.get("/debug/state-check").json()["replay_matches_snapshot"]
