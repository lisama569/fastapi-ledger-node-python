# 本地简化链节点（FastAPI + 内存账本）

在现有 FastAPI 骨架上新增的一套**本地简化链节点能力**：交易入池、出块、链头切换（分叉）全部在本地内存完成，
不涉及真实网络与真实密码学（块/交易 id 用字段的 sha256 摘要代替签名）。重启进程数据丢失，仅用于本地验证。

## 目录结构

```
node/
  errors.py       # 错误原因枚举与异常：拒绝原因可区分
  models.py       # Transaction / Block / AccountState（内容寻址 id）
  state.py        # LedgerState：余额、nonce、总量、交易应用、快照
  mempool.py      # Mempool：线程安全的待处理队列，不覆盖不顶掉
  blockchain.py   # Chain：块树、按块快照、权重选链、独立重放
  node.py         # Node：门面，一把 RLock 串行化所有公开操作
main.py           # HTTP 接口（单节点内存实例，可 /admin/reset 创世）
tests/            # 提交/出块/分叉/API 四类测试（带输入与判定依据打印）
```

## 1. 账户、交易与校验规则

- 账户状态 = `balance`（整数，最小单位）+ `nonce`（已执行交易数，下一笔期望序号就是它）。
- 交易字段：`sender, recipient, nonce, amount, fee`。
  - `amount > 0`，`fee >= 0`，`nonce >= 0`，收发方非空，否则 `INVALID_TRANSACTION`。
  - 交易 id = sha256(字段) ；内容完全相同即同一笔。
- 提交校验顺序（`Node.submit_tx`，全流程在同一把锁内，任何一步失败都不改池）：
  1. **字段合法** → 否则 `INVALID_TRANSACTION`；
  2. **序号不低于链上 nonce** → `NONCE_TOO_LOW`（说明该序号已上链，即重复提交历史交易）；
  3. **序号不断层**：下一个期望序号 = 链上 nonce + 池中该账户挂起交易数，
     提交序号更大 → `NONCE_GAP`；
  4. **余额足**：可用余额 = 链上余额 − 池中该账户全部挂账（amount+fee 之和），
     不够 → `INSUFFICIENT_FUNDS`（防止多笔单笔够、合计不够）；
  5. **入池去重**：同 tx_id，或同 `(sender, nonce)` 已有不同交易 → `DUPLICATE_TX`，
     **绝不静默覆盖或顶掉**池里已有的交易。
- 注意：序号断层是相对于"链上 + 池中连续前缀"而言。例如池里没有 nonce=0 时直接提 1，是断层；
  断层交易被拒后仍可提交 nonce=0。

## 2. 出块与选交易规则（明确且稳定）

`Node.produce_block(producer)`：

1. 取当前链头状态的拷贝作为工作状态；
2. **逐轮**把池子里的候选按 `(fee 降序, tx_id 升序)` 扫描：
   - 该轮扫描中，凡在当前工作状态下**可执行**（nonce 精确匹配、余额够付 amount+fee）
     的交易，按扫描到的先后**追加**进块，并立即应用到工作状态；
   - 不可执行（如同账户前序 nonce 尚未入块、或余额因本块先前交易不足）先跳过；
3. 反复扫描，直到某一轮没有任何新交易入选，或达到 `max_block_txs`（默认 100）；
4. 以块的内容计算 sha256 得到 `block_id`，挂在当前链头之后，**严格模式**（整块有非法即拒）
   接入链；成功后把已打包交易移出池子。

> 为什么要逐轮而不是只扫一遍：高手续费的 nonce=1 必须等本账户 nonce=0 在同块先执行。
> 入块顺序 = 逐轮追加的顺序，因此块按该顺序应用时每一笔都可执行，不存在"选出来却执行不了"。
> 留在池中未入选的交易（比如当前余额真的不够）下次出块还会再尝试。

块的应用（`Chain.add_block`）对每笔交易：`sender` 扣 `amount+fee`、nonce+1；
`recipient` 加 `amount`；`producer` 加 `fee`。手续费只是账户间转移，**不增发**。
每应用完一个块断言 `total_supply == 创世总量`。所有校验先于状态修改，
单个块的处理在工作拷贝上进行，失败直接丢弃拷贝——不会扣一半，也不会算两次。

块内混入非法交易时由节点策略决定，结果里必须说得清是哪一种：

| 策略 | 行为 | 返回 |
|---|---|---|
| `REJECT_WHOLE_BLOCK`（默认） | 任一笔非法（字段/nonce/余额）整块拒绝，一笔都不应用 | 抛 `INVALID_BLOCK`，detail 指明是第几笔、什么原因 |
| `SKIP_INVALID_TX` | 逐笔判定，非法单笔剔除，其余正常应用 | 返回 `skipped: [(tx_id, reason), ...]` |

此外：父块不存在 → `UNKNOWN_PARENT`（孤儿块）；高度不等于父高+1 → `INVALID_HEIGHT`；
同一 `block_id` 再送 → `DUPLICATE_BLOCK`（不会重复计入状态）。

## 3. 链头切换（分叉）语义

- 块以 `parent_id` 组成一棵从创世（height=0）生长的树；**每个块入链时保存其应用后的完整状态快照**。
- **选链规则**：权重 = `height + 1`（链越长权重越大）。新块权重**严格大于**当前链头才切换；
  相等不切换——**先到先得**。无论是否切换，块和它的快照都会被保存。
- 切换后：
  - 链头状态直接取新块的快照；该快照与"从创世沿新链把每笔交易顺序重放一遍"逐账户一致
    （`Chain.replay_state` 独立计算，`GET /blocks/{id}/verify-replay` 可在线比对）；
  - **被切走分支的块与状态不删除**，`state_at(block_id)` / HTTP 仍可查回；
  - 旧分支独有块里的交易（自共同祖先向外、按原顺序）**重新放回交易池**等待重新打包；
    已在新链落地的同 nonce 交易不回池。这样"切走的分支"上的交易不会丢，且状态里只计一次。

## 4. 错误分类

`node/errors.py::RejectReason`：

| reason | 场景 |
|---|---|
| `DUPLICATE_TX` | 同一笔交易，或同 账户+nonce 的不同交易，已在池中 |
| `NONCE_TOO_LOW` | nonce 小于账户在链上的已用 nonce（历史交易重复提交） |
| `NONCE_GAP` | nonce 大于"链上 nonce + 池中连续挂起数"，序号断层 |
| `INSUFFICIENT_FUNDS` | 余额（扣除池中挂账后）不足支付 amount+fee |
| `INVALID_TRANSACTION` | 金额<=0、手续费<0、nonce<0、账户名为空 |
| `UNKNOWN_PARENT` | 块的父块不存在（孤儿块） |
| `DUPLICATE_BLOCK` | 同一区块重复送达 |
| `INVALID_BLOCK` | 严格策略下块内存在非法交易 |
| `INVALID_HEIGHT` | 块高与父块不连续 |

HTTP 层：交易/块被拒返回 **409**，body 为 `{"detail": {"reason": ..., "message": ...}}`；
查不存在的块返回 404。

## 5. HTTP 接口

| 方法 路径 | 作用 |
|---|---|
| `POST /admin/reset` | 重建内存节点（创世分配、策略、块大小上限） |
| `GET  /state` | 链头状态：各账户 balance/nonce、总量、待处理数 |
| `POST /txs` | 提交交易入池 |
| `POST /blocks/produce` | 本节点选交易出块 |
| `POST /blocks` | 接收外部区块（返回 switched_head / skipped） |
| `GET  /blocks/{id}/state` | 任意块（含旧分支）之后的状态快照 |
| `GET  /blocks/{id}/verify-replay` | 独立重放与快照一致性比对 |

## 6. 本地验证方式

```bash
uv sync                                  # 安装依赖（含 pytest）
uv run pytest -s                         # 跑全部测试并打印输入与判定依据
uv run pytest tests/test_tx_submission.py -s   # 只看并发提交/错误分类
uv run pytest tests/test_fork.py -s            # 只看分叉切换/重复回放

uv run python main.py                    # 起服务，默认 127.0.0.1:8000
# 然后用 /docs 的 Swagger UI，或 curl：
curl -s localhost:8000/state
curl -s -XPOST localhost:8000/txs -H 'content-type: application/json' \
  -d '{"sender":"alice","recipient":"bob","nonce":0,"amount":100,"fee":2}'
curl -s -XPOST localhost:8000/blocks/produce -H 'content-type: application/json' \
  -d '{"producer":"miner"}'
```

测试覆盖：

- **并发提交**（10×2 个线程打同一组 nonce）：同 账户+nonce 恰好一笔入池，
  拒绝只可能是 `DUPLICATE_TX` / `INSUFFICIENT_FUNDS` / `NONCE_GAP`，
  入池 nonce 必为连续前缀，出块后总量守恒；
- **分叉切换**：更长分支接入后链头切换，快照 == 独立重放，被切走分支可查、
  旧交易回池重打包且只计一次，等权重保持先到先得；
- **重复回放/重复送达**：同一区块送两次报 `DUPLICATE_BLOCK` 且手续费不重复计；
  并发接块后链头快照与任一线性化重放一致；
- 各种非法输入以**可区分 reason** 被稳定拒绝，`-s` 会打印每笔输入与判定依据。
