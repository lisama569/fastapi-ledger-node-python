"""FastAPI 适配层：HTTP JSON <-> LedgerNode。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .chain import SKIP, STRICT
from .errors import LedgerError, RejectReason
from .node import LedgerNode

DEFAULT_ALLOCATIONS = {"alice": 1_000, "bob": 500, "carol": 100}

_BAD_REQUEST_REASONS = {
    RejectReason.BAD_INPUT,
    RejectReason.BAD_BLOCK_HASH,
    RejectReason.BAD_BLOCK_STRUCTURE,
}


def create_app(allocations: dict[str, int] | None = None) -> FastAPI:
    app = FastAPI(title="local-ledger-node")
    node = LedgerNode(allocations if allocations is not None else DEFAULT_ALLOCATIONS)
    app.state.node = node

    @app.exception_handler(LedgerError)
    async def ledger_error_handler(_request: Request, exc: LedgerError):
        status = 400 if exc.reason in _BAD_REQUEST_REASONS else 409
        return JSONResponse(
            status_code=status,
            content={
                "status": "error",
                "reason": str(exc.reason),
                "message": exc.message,
                "details": exc.details,
            },
        )

    @app.get("/")
    def root():
        return {
            "service": "local-ledger-node",
            "genesis_allocations": app.state.node.allocations,
            "docs": "见 README.md",
        }

    @app.get("/accounts/{name}")
    def get_account(name: str):
        return app.state.node.account(name)

    @app.get("/accounts")
    def list_accounts():
        n = app.state.node
        return {
            "tip_id": n.chain.tip_id,
            "accounts": [
                n.account(name) for name in sorted(n.chain.tip_state.accounts)
            ],
            "total_supply": n.total_supply(),
        }

    @app.get("/mempool")
    def get_mempool():
        return {"pending": app.state.node.pending()}

    @app.post("/transactions")
    def submit_transaction(payload: dict):
        return app.state.node.submit_tx(payload)

    @app.post("/blocks/produce")
    def produce(payload: dict):
        producer = payload.get("producer")
        weight = payload.get("weight", 1)
        return app.state.node.produce_block(producer, weight)

    @app.post("/blocks/receive")
    def receive(payload: dict):
        policy = payload.get("policy", STRICT)
        block_data = payload.get("block", payload)
        return app.state.node.receive_block(block_data, policy)

    @app.get("/chain/tip")
    def get_tip():
        return app.state.node.tip()

    @app.get("/blocks/{block_id}")
    def get_block(block_id: str):
        return app.state.node.block(block_id)

    @app.get("/debug/state-check")
    def state_check():
        return app.state.node.state_check()

    return app


app = create_app()
