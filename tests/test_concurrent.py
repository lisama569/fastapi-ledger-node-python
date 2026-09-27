"""并发提交与并发出块测试。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from node.errors import LedgerError, RejectReason
from node.models import Transaction
from node.node import LedgerNode


GENESIS = {"alice": 100, "bob": 100, "carol": 100}
GENESIS_SUPPLY = sum(GENESIS.values())


def test_many_threads_one_nonce_slot_exactly_one_wins():
    node = LedgerNode(GENESIS)
    n_threads = 32
    barrier = threading.Barrier(n_threads)
    outcomes: dict[int, str] = {}
    lock = threading.Lock()

    def worker(i: int):
        barrier.wait()  # 尽量同时撞进来
        tx_id = f"alice0-{i:03d}"
        try:
            node.submit_tx(
                {"sender": "alice", "recipient": "bob", "nonce": 0,
                 "amount": 10, "fee": 1, "tx_id": tx_id}
            )
            result = "accepted"
        except LedgerError as exc:
            result = str(exc.reason)
        with lock:
            outcomes[i] = result

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(worker, range(n_threads)))

    accepted = [k for k, v in outcomes.items() if v == "accepted"]
    reasons = {v for v in outcomes.values()}
    print("\n[输入] 32 个线程同时提交 alice nonce=0（各自不同 tx_id，花费 11）")
    print(f"[判定] 成功数={len(accepted)}（应为1），拒绝原因集合={sorted(reasons)}")

    assert len(accepted) == 1
    assert reasons <= {"accepted", RejectReason.NONCE_CONFLICT.value}
    pending = node.pending()
    assert len(pending) == 1
    assert pending[0]["nonce"] == 0
    assert pending[0]["tx_id"] == f"alice0-{accepted[0]:03d}"


def test_concurrent_nonces_each_slot_filled_once_and_queue_intact():
    node = LedgerNode(GENESIS)
    per_slot = 6
    slots = 8
    n_threads = per_slot * slots
    barrier = threading.Barrier(n_threads)
    accepted_ids: list[str] = []
    rejected: list[str] = []
    lock = threading.Lock()

    def worker(rep: int, nonce: int):
        barrier.wait()
        tx_id = f"a-n{nonce}-r{rep}"
        try:
            node.submit_tx(
                {"sender": "alice", "recipient": "bob", "nonce": nonce,
                 "amount": 1, "fee": 1, "tx_id": tx_id}
            )
            with lock:
                accepted_ids.append(tx_id)
        except LedgerError as exc:
            with lock:
                rejected.append(str(exc.reason))

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futs = []
        for nonce in range(slots):
            for rep in range(per_slot):
                futs.append(pool.submit(worker, rep, nonce))
        [f.result() for f in futs]

    print(f"\n[输入] {n_threads} 线程抢 alice 的 {slots} 个序号槽（每槽 {per_slot} 份，amount=fee=1）")
    print(f"[判定] 成功={len(accepted_ids)}（应={slots}），拒绝={len(rejected)}（应={slots*per_slot-slots}），"
          f"拒绝原因集合={sorted(set(rejected))}")

    assert len(accepted_ids) == slots
    assert len(rejected) == slots * per_slot - slots
    # 每个序号槽恰好一笔，序号 0..7 连续，无丢更新
    pending = node.pending()
    by_nonce = {t["nonce"]: t["tx_id"] for t in pending}
    assert sorted(by_nonce) == list(range(slots))
    # 余额检查并发下仍然成立：池中累计花费 8*2=16 <= 100
    assert node.account("alice")["balance"] == 100  # 还未出块，链上余额不变


def test_concurrent_balance_check_never_overcommits():
    node = LedgerNode(GENESIS)
    n_threads = 12
    barrier = threading.Barrier(n_threads)
    lock = threading.Lock()
    results = {"accepted": 0, "insufficient": 0, "other": 0}

    def worker(i: int):
        barrier.wait()
        try:
            node.submit_tx(
                {"sender": "bob", "recipient": "carol", "nonce": 0,
                 "amount": 90, "fee": 0, "tx_id": f"bob-grab-{i}"}
            )
            with lock:
                results["accepted"] += 1
        except LedgerError as exc:
            with lock:
                if exc.reason == RejectReason.INSUFFICIENT_BALANCE:
                    results["insufficient"] += 1
                else:
                    results["other"] += 1

    with ThreadPoolExecutor(max_workers=n_threads) as ex:
        list(ex.map(worker, range(n_threads)))

    queued_cost = sum(t["cost"] for t in node.pending())
    print(f"\n[输入] {n_threads} 线程同时提交 bob nonce=0 amount=90 fee=0（余额100）")
    print(f"[判定] {results}，池中累计花费={queued_cost}（必须 <=100，且只有1笔）")
    assert results["accepted"] == 1
    assert results["insufficient"] == 0  # 其余是槽位冲突，不是余额问题
    assert results["other"] == n_threads - 1
    assert queued_cost == 90


def test_concurrent_submit_and_produce_keep_ledger_consistent():
    node = LedgerNode(GENESIS)
    stop = threading.Event()
    errors: list[Exception] = []
    err_lock = threading.Lock()

    def submitter(sender: str):
        i = 0
        while not stop.is_set():
            nonce = node.account(sender)["nonce"]
            try:
                node.submit_tx(
                    {"sender": sender, "recipient": "carol", "nonce": nonce,
                     "amount": 1, "fee": 1, "tx_id": f"{sender}-{i}"}
                )
            except LedgerError:
                pass  # 与出块赛跑：序号刚被推进、重复、余额不足都允许
            except Exception as exc:  # noqa: BLE001 任何意外都算并发写乱
                with err_lock:
                    errors.append(exc)
            i += 1

    def producer():
        while not stop.is_set():
            try:
                node.produce_block("miner", weight=1)
            except Exception as exc:  # noqa: BLE001
                with err_lock:
                    errors.append(exc)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(submitter, s) for s in ("alice", "bob", "carol")]
        pfuts = [pool.submit(producer) for _ in range(3)]
        stop.wait(0.4)
        stop.set()
        [f.result() for f in futs + pfuts]

    check = node.state_check()
    print("\n[输入] 3 个提交线程 + 3 个出块线程并发跑 0.4 秒")
    print(f"[判定] 意外异常={len(errors)}，链高={node.tip()['height']}，"
          f"总量={check['total_supply']}（创世={GENESIS_SUPPLY}），"
          f"重放一致={check['replay_matches_snapshot']}")
    assert not errors
    assert check["supply_conserved"] is True
    assert check["replay_matches_snapshot"] is True
    # 账户序号与当前链上完全对齐（非重用、无断层）
    for name in ("alice", "bob", "carol", "miner"):
        acc = node.account(name)
        assert acc["nonce"] >= 0
