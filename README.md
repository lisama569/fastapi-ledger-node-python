# 本地简化链节点（in-memory）

在原有 FastAPI 服务上新增的一套**本地内存链节点能力**：交易入池、出块、收块、
分叉链头切换与状态回放，全部在进程内存中完成，不涉及真实网络与真实密码学。

- 区块"身份"= 对区块规范内容（height/parent_id/producer/weight/transactions）
  做 SHA-256 摘要，用来判断"同一份块"（防内容错配、保证重复投递幂等）。
- 全部写操作由 `LedgerNode` 内一把可重入锁 `RLock` 保护，并发请求等价于
  串行化执行，不会把待处理队列或状态写乱。
- 没有区块奖励（不增发）。手续费在每块内从发送方转给该块 `producer`，
  因此**所有状态下总余额恒等于创世分配总量**。

## 目录结构

| 文件 | 职责 |
| --- | --- |
| `node/models.py` | 交易/账户/区块/收据数据类型、规范编码、内容摘要、入参结构校验 |
| `node/state.py` | 世界状态（余额、序号），单笔交易**原子**应用与快照 |
| `node/mempool.py` | 待处理交易池：提交校验、去重、稳定选交易规则、切换后重校 |
| `node/chain.py` | 区块/状态快照存储、strict/skip 执行、链头切换、从创世重放 |
| `node/node.py` | 加锁门面，供 HTTP 与测试调用 |
| `node/api.py` | FastAPI 路由与错误到 HTTP 的映射 |
| `tests/` | 交易池、并发、出块、分叉/重放、HTTP 共 21 个用例 |

## 交易与账户模型

- 账户：`balance`（非负整数余额）、`nonce`（下一个可用序号，从 0 开始）。
- 交易：`sender, recipient, nonce, amount, fee, tx_id`，均为必填；
  `amount/fee/nonce` 必须是非负整数，`sender/recipient/tx_id` 为非空字符串。
- 一笔交易的花费 = `amount + fee`，必须由发送方全额承担。
  收款方不存在时自动建零余额账户；发送方不存在直接拒绝。
- 自转给自己是允许的：金额先扣后加抵消，手续费正常支付。

## 交易提交校验（`POST /transactions`）

按下面顺序判定，每个槽位每笔交易都有**可区分的拒绝原因**，
被拒绝不会改动池子，也不会顶掉已有交易：

1. 发送方账户不存在 → `unknown_sender`
2. `(sender, nonce)` 槽位已有交易：
   - 就是同一个 `tx_id`（完全重复提交）→ `duplicate_tx`
   - 是另一个 `tx_id`（同序号竞争）→ `nonce_conflict`
3. `nonce < 账户链上 nonce` → `nonce_stale`
4. `nonce > 账户链上 nonce + 该账户已在池中的交易数` → `nonce_gap`
   （只接受"紧贴队尾"的序号，禁止断层）
5. 链上余额 < 池中更早序号交易累计花费 + 本笔花费 → `insufficient_balance`
6. 字段缺失/类型错误/负数 → `bad_input`

并发语义：多线程同时提交同一 `(sender, nonce)` 槽位时，恰好一笔成功，
其余全部得到 `nonce_conflict`；成功笔数、槽位数量、累计花费在并发下严格一致
（见 `tests/test_concurrent.py`）。

## 出块与选交易规则（`POST /blocks/produce`）

1. 对池中全部候选交易按 **`fee` 降序**排序；`fee` 相同按 **`tx_id` 字典序升序**。
   排序键与提交顺序、到达时刻无关，因此选交易结果是**确定**的。
2. 按上述顺序在父状态的工作副本上**逐笔单遍试应用**：
   - 试应用成功 → 收录进本块；
   - 试应用失败（同一账户高序号先被扫到导致的临时断层、本块内累计余额不足）
     → **跳过并继续留在池里**，等下一个块。
3. 被选中的交易构造为一个块，统一走 `strict` 执行路径入账，随后从池中删除。

注意（刻意的稳定规则）：高手续费但序号靠后的交易，在其前序交易未上链前
会在单遍扫描中被跳过；一旦前序交易在同一块中先被收录，后续扫描到它时即可通过。
跳过交易不会被丢弃，下一个块重新参与同一套排序。空块允许产生。

## 收块与非法交易策略（`POST /blocks/receive`）

接收外部块时先做结构校验：

- 携带的 `block_id` 与按内容重算的摘要不一致 → `bad_block_hash`
- 父块未知（暂不缓存孤儿块）→ `unknown_parent`
- 高度不等于父块高度 + 1 → `bad_block_structure`
- 同一块内出现重复交易（同 `tx_id` 或同 `(sender,nonce)`）→ `duplicate_tx_in_block`
- 字段结构错误 → `bad_block_structure` / `bad_input`

业务执行提供两种**显式可选**策略，结果里会标明走的是哪一种：

- `strict`（默认）：块中任一笔交易非法（含交易已在父链上生效的 `already_applied`），
  **整块拒绝**：一笔都不扣、状态完全不动、块和快照都不存储，
  返回 `block_has_invalid_tx` 及"首笔非法交易 + 原因"。
- `skip`：逐笔在临时状态上试跑，非法交易**单笔剔除**（结果的 `excluded` 里给出
  每笔的 tx_id 与原因），合法交易作为一个整体提交；不会出现扣了一半，
  因为任何一笔失败都只发生在随后被丢弃的临时副本上。

两种策略都保证：合法交易最多计入一次、非法交易从不计入。

## 链头切换（分叉）语义

- 每个块带正整数 `weight`；一条链的权重 = 创世到链头各块权重之和。
- 收到合法块后，**仅当候选链累计权重严格大于当前链头**才切换；
  权重相同不切换（先到为准，保证确定性）。
- 所有分叉块与其执行后的状态快照都会保留，因此**被切走分支上的状态随时能找回**
  （`GET /blocks/{id}`、内部 `state_at(id)`），也可以在之后权重反超时再切回去。
- 切换链头后，交易池以新链头状态为基准逐笔重校：
  与新链冲突（如序号已被新链用掉）的交易被丢弃，原因记录在接收响应的
  `dropped_from_mempool` 中；其余交易继续排队。
- **回放等价**：切换后链头状态必须等于"不用任何缓存、从创世块开始沿新链
  逐笔重放被收录交易"的结果。`skip` 块只重放其 `included` 集合
  （剔除清单与块一起持久化）。可用 `GET /debug/state-check` 随时核对
  `replay_matches_snapshot` 与总量守恒。
- 同一份块重复投递返回 `duplicate_block` 且完全幂等：不重复入账、不产生第二条
  存储记录；如果该块所在链此时权重更大，仍会正常触发一次链头切换。

## 错误分类一览

| reason | 含义 | 典型 HTTP |
| --- | --- | --- |
| `bad_input` | 字段缺失/类型错/非负整数约束等 | 400 |
| `bad_block_hash` | 携带的 block_id 与内容摘要不符 | 400 |
| `bad_block_structure` | 高度不连续、父链成环、结构非法 | 400 |
| `unknown_sender` | 发送方账户不存在 | 409 |
| `duplicate_tx` | 同一 tx_id 重复提交 | 409 |
| `nonce_conflict` | 同 `(sender,nonce)` 槽位被另一笔交易占用 | 409 |
| `nonce_stale` | 序号已在链上使用过 | 409 |
| `nonce_gap` | 序号断层（池槽不连续 / 执行时前序缺失） | 409 |
| `insufficient_balance` | 余额（含队列中更早交易花费）不足 | 409 |
| `already_applied` | 交易已在父链上生效，又被塞进后续块 | 见块策略 |
| `duplicate_tx_in_block` | 同一块内重复交易 | 400 |
| `block_has_invalid_tx` | strict 块含非法交易，整块拒绝 | 200/业务拒绝体 |
| `duplicate_block` | 同一内容块重复投递（幂等，不重复计入） | 200/duplicate 体 |
| `unknown_parent` | 父块未知 | 400 |

HTTP 层：纯输入/结构问题返回 400，业务冲突（重复、序号、余额等）返回 409；
收块的"整块拒绝/重复块"仍返回 200，用响应体的 `status/reject_reason` 表达，
便于同时拿到收据明细。

## HTTP 接口

| 方法路径 | 说明 |
| --- | --- |
| `GET /` | 服务信息与创世分配 |
| `GET /accounts` / `GET /accounts/{name}` | 链头账户列表 / 单账户（含池中排队） |
| `GET /mempool` | 按出块排序规则返回待处理交易 |
| `POST /transactions` | 提交交易 |
| `POST /blocks/produce` | 体 `{"producer": "...", "weight": 1}` 本地出块 |
| `POST /blocks/receive` | 体 `{"policy": "strict|skip", "block": {...}}` 收块 |
| `GET /chain/tip` | 当前链头与累计权重 |
| `GET /blocks/{block_id}` | 任意分叉块内容与执行收据（含/剔除明细） |
| `GET /debug/state-check` | 总量守恒 + 重放一致性 + 活动链自检 |

## 本地验证

```bash
# 运行测试（已配置 addopts=-v -rA -s，会打印每个用例的输入与判定依据）
python3 -m pytest

# 启动服务
python3 main.py            # http://127.0.0.1:8000，文档 /docs
```

手工试一条链路：

```bash
curl -s localhost:8000/accounts
curl -s -X POST localhost:8000/transactions -H 'content-type: application/json' \
  -d '{"sender":"alice","recipient":"bob","nonce":0,"amount":10,"fee":5,"tx_id":"t1"}'
curl -s -X POST localhost:8000/blocks/produce -H 'content-type: application/json' \
  -d '{"producer":"miner"}'
curl -s localhost:8000/debug/state-check
```

测试覆盖对应关系：

- 并发提交：`tests/test_concurrent.py`（抢同一序号槽、多槽竞争、并发余额校验、
  提交与出块并发下的守恒/重放一致）。
- 分叉切换：`tests/test_fork.py`（重链切换、等重不切、来回切换、旧分支找回）。
- 重复回放：`tests/test_fork.py` / `tests/test_block.py`
  （重复块幂等、skip 块只计 included、多次从创世重放结果相同）。
- 非法输入的可区分原因：`tests/test_mempool.py`、`tests/test_block.py`。
