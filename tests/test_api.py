"""HTTP 接口冒烟：提交拒绝原因、出块、收块切换、回放校验。"""
from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

import main


@pytest.fixture()
def client() -> TestClient:
    main.get_node()  # 触发模块加载
    return TestClient(main.app)


def reset(client: TestClient, alloc: dict[str, int], policy: str = "REJECT_WHOLE_BLOCK"):
    r = client.post("/admin/reset", json={"genesis_alloc": alloc, "policy": policy})
    assert r.status_code == 200, r.text
    return r.json()["genesis_id"]


def test_submit_rejections_over_http(client: TestClient) -> None:
    reset(client, {"alice": 100})
    r1 = client.post("/txs", json={"sender": "alice", "recipient": "bob",
                                   "nonce": 2, "amount": 10, "fee": 0})
    print("断层提交:", r1.status_code, r1.json()["detail"])
    assert r1.status_code == 409
    assert r1.json()["detail"]["reason"] == "NONCE_GAP"
    client.post("/txs", json={"sender": "alice", "recipient": "bob",
                              "nonce": 0, "amount": 10, "fee": 1})
    r2 = client.post("/txs", json={"sender": "alice", "recipient": "carol",
                                   "nonce": 0, "amount": 10, "fee": 1})
    print("同 nonce 冲突:", r2.status_code, r2.json()["detail"])
    assert r2.json()["detail"]["reason"] == "DUPLICATE_TX"
    r3 = client.post("/txs", json={"sender": "alice", "recipient": "bob",
                                   "nonce": 1, "amount": 9999, "fee": 0})
    print("余额不足:", r3.status_code, r3.json()["detail"])
    assert r3.json()["detail"]["reason"] == "INSUFFICIENT_FUNDS"


def test_produce_and_inspect_state(client: TestClient) -> None:
    reset(client, {"alice": 100})
    client.post("/txs", json={"sender": "alice", "recipient": "bob",
                              "nonce": 0, "amount": 30, "fee": 2})
    r = client.post("/blocks/produce", json={"producer": "miner"})
    assert r.status_code == 201, r.text
    block = r.json()
    print("出块:", block["height"], len(block["txs"]), "笔")
    assert block["height"] == 1
    st = client.get("/state").json()
    print("状态:", st)
    assert st["accounts"]["alice"]["balance"] == 68
    assert st["accounts"]["miner"]["balance"] == 2
    assert st["total_supply"] == 100


def test_receive_block_with_invalid_tx_whole_reject(client: TestClient) -> None:
    g = reset(client, {"alice": 100}, "REJECT_WHOLE_BLOCK")
    payload = {
        "height": 1, "parent_id": g, "producer": "evil",
        "txs": [
            {"sender": "alice", "recipient": "bob", "nonce": 0, "amount": 10, "fee": 0},
            {"sender": "alice", "recipient": "bob", "nonce": 2, "amount": 10, "fee": 0},
        ],
    }
    r = client.post("/blocks", json=payload)
    print("含非法交易整块拒绝:", r.status_code, r.json()["detail"]["reason"])
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "INVALID_BLOCK"
    assert client.get("/state").json()["accounts"]["alice"]["balance"] == 100


def test_receive_block_skip_policy_and_replay_verify(client: TestClient) -> None:
    g = reset(client, {"alice": 100}, "SKIP_INVALID_TX")
    payload = {
        "height": 1, "parent_id": g, "producer": "miner",
        "txs": [
            {"sender": "alice", "recipient": "bob", "nonce": 0, "amount": 10, "fee": 1},
            {"sender": "alice", "recipient": "bob", "nonce": 5, "amount": 10, "fee": 0},
        ],
    }
    r = client.post("/blocks", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    print("SKIP 收块:", body)
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["reason"] == "NONCE_GAP"
    v = client.get(f"/blocks/{body['block_id']}/verify-replay")
    print("独立重放比对:", v.json())
    assert v.json()["matches"] is True


def test_duplicate_block_delivery(client: TestClient) -> None:
    g = reset(client, {"alice": 100})
    payload = {"height": 1, "parent_id": g, "producer": "m", "txs": []}
    first = client.post("/blocks", json=payload)
    assert first.status_code == 201
    second = client.post("/blocks", json=payload)
    print("重复送块:", second.status_code, second.json()["detail"]["reason"])
    assert second.json()["detail"]["reason"] == "DUPLICATE_BLOCK"


def test_old_branch_state_fetchable(client: TestClient) -> None:
    g = reset(client, {"alice": 1000, "bob": 100})
    # A1 高度1
    a1 = client.post("/blocks", json={"height": 1, "parent_id": g, "producer": "pA",
                                      "txs": [{"sender": "alice", "recipient": "bob",
                                               "nonce": 0, "amount": 100, "fee": 1}]}).json()
    # B 分支高度2
    b1 = client.post("/blocks", json={"height": 1, "parent_id": g, "producer": "pB",
                                      "txs": [{"sender": "bob", "recipient": "alice",
                                               "nonce": 0, "amount": 10, "fee": 1}]}).json()
    b2 = client.post("/blocks", json={"height": 2, "parent_id": b1["block_id"],
                                      "producer": "pB",
                                      "txs": [{"sender": "bob", "recipient": "alice",
                                               "nonce": 1, "amount": 10, "fee": 1}]}).json()
    print(f"A1={a1['switched_head']} B1={b1['switched_head']} B2={b2['switched_head']}")
    assert b2["switched_head"] is True
    # A1 虽被切走，状态仍可查
    r = client.get(f"/blocks/{a1['block_id']}/state")
    print("旧分支 A1 状态:", r.json()["accounts"])
    assert r.status_code == 200
    assert r.json()["accounts"]["alice"]["balance"] == 899
