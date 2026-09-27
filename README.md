# PySpreadBot

基于 Python 3.11+ / `asyncio` 的跨交易所线性永续合约套利程序。首批接入 Gate USDT 合约和 Hyperliquid 原生 USDC 永续。源码直接放在 `src/`，打包后包名为 `pyspreadbot`。

默认 `paper`：接收真实行情，在本地模拟成交，不向交易所发送订单。`observe` 只发现机会；`live` 使用真实账户。实盘协议实现已经提供，但未使用真实账户验证签名、权限、余额、费率和成交回报，不能将离线测试视为实盘验收。

## 安装与启动

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
# 首次配置；已有 .env 时请合并新增项，不要覆盖密钥
cp .env.example .env
# 需要已安装并启动 Docker，也可自行提供 Redis 7 服务
docker compose up -d redis
python src/main.py cache
python src/main.py run
```

也支持 `pyspreadbot run` 和 `python -m pyspreadbot run`。所有命令默认从当前工作目录读取 `.env`；可通过 `--env /path/to/config.env` 指定文件。

```sh
python src/main.py status  # 查看全局归属、持仓及预计平仓 PnL
python src/main.py probe   # 只读验证两个交易所的 BTC WSS 行情和深度，无需 Redis/密钥
pytest -q                 # 离线测试，不连接交易所、不下单
ruff check src tests
uv build                  # 打包至 dist/
```

初始监控 `BTC,ETH`，修改 `SYMBOLS=*` 可监控双方所有共同标的。`cache` 获取全部受支持合约的规则，不受 `SYMBOLS` 过滤。规则写入 `data/contracts/gate.json` 和 `hyperliquid.json`，同时保存规范字段和原始响应。启动和定期刷新必须成功取得新规则；旧缓存用于检查，不用于离线实盘下单。

## 单独调试 Gate（PyCharm）

直接运行或 Debug `src/exchange/gate.py`（也可运行 `src/debug_gate.py`），使用项目 `.venv` 解释器即可。默认读取项目根目录 `.env`，单进程持续订阅 BTC 公共行情及深度，沿用 HTTP/WSS 代理，不需要 Redis 或交易密钥，不执行交易。

```sh
python src/exchange/gate.py --contracts-only  # 只初始化并缓存合约，适合调试 fetch_instruments
python src/exchange/gate.py --symbol BTC      # 持续接收，适合在 handle_message 中打断点
python src/exchange/gate.py --symbol BTC --once  # 收到首个新鲜盘口后退出
```

PyCharm 的 Parameters 可填写上述选项。该入口强制 `observe`，即使 `.env` 配置 `MODE=live` 也只读；Ctrl+C 时清理连接。暂停断点可能触发 WSS 心跳超时，继续运行后会自动重连。

## 数据与执行流程

1. 读取合约元数据：最小/最大数量、数量步长、合约乘数、价格精度、下架状态、价格偏离限制等。
2. WSS 维护轻量行情：Gate 买卖一及标记价、Hyperliquid 全市场中间价。
3. 初筛出现价格差后，通过 WSS 订阅该标的深度。Gate 使用 `futures.obu` 50 档，Hyperliquid 使用 `l2Book`。长期不满足初筛的空仓标的自动退订深度。
4. 两边逐档匹配可成交数量，检查共同数量步长、手续费、往返滑点预算、规模上限、信号持续时间和行情新鲜度。
5. Redis 原子获取基础币归属；确认账户没有遗留仓位，两边基础校验均通过，发送前再次核对可成交价差。
6. 先写订单 intent，再通过两边 WSS 并发发送 IOC 限价单；查明最终成交。多成交的一边以 `reduce-only` 削减差额。
7. 对账并维护持仓，持续计算当前双边反向深度可实现的预计 PnL，触发止盈/止损时平仓。确认双方实际仓位均为零后释放归属。

`Order` 在策略、执行器和交易所间流转；每个适配器负责校验规则、原始参数转换及协议解析。所有金额使用 `Decimal`。本版只通过减仓修正双腿不平衡，不通过追单加仓扩大风险。

热路径行情、深度、下单、订单查询均使用 WSS。HTTP 用于低频元数据获取，以及 Gate 实际仓位核对（其 WSS 没有对应的仓位查询请求接口）。网络延迟、交易所推送周期、签名和 Redis 持久化都会影响端到端延迟，本项目不承诺毫秒级成交。

## 单位及汇率

展示名称保留真实报价币，例如 `BTC-USDT`、`BTC-USDC`；跨交易所按基础币 `BTC` 匹配，所有价格统一折算为 USD 估值。不能通过直接把 USDC 改名成 USDT 消除基差。

```dotenv
# 每单位报价币的 USD 估值，以下仅为配置示例，并非实时汇率
USDT_USD=1
USDC_USD=0.999
# 特殊单位必须显式指定；内置 kPEPE 等已知 Hyperliquid 千倍币映射
ALIASES={"gate:1000PEPE_USDT":["PEPE","1000"]}
```

- 统一价格 = 原始价格 × 报价币 USD 折算率 ÷ 单位倍数。
- 统一数量 = 原始下单数量 × 每张合约对应数量 × 单位倍数。
- `1INCH` 等名称中的数字不会被盲目剥离；未知千倍币通过 `ALIASES` 配置。
- 两边基础币步长使用最小公倍数计算，不直接取较大的步长。

汇率在进程启动时读取，不自动采集 FX，也不在持仓中途热更新。`.env.example` 的两个 `1` 是默认示例；请填写需要的折算率。运行期 PnL 使用这组固定折算率。

## 主要配置

HTTP 和 WSS 代理分别配置，缺省、留空或 `None`（不区分大小写）表示直连：

```dotenv
HTTP_PROXY=http://127.0.0.1:7890
WSS_PROXY=http://127.0.0.1:7890
```

不使用代理时将两项均设为 `None`。WSS 使用 HTTP CONNECT 代理地址，不填写 `wss://`；支持 HTTP 代理，HTTPS 代理支持取决于运行环境，不支持 SOCKS 地址。两个配置互不继承，WSS 重连仍使用其配置的代理。Redis 不受这两个配置影响。

| 项目 | 含义 |
| --- | --- |
| `MODE` | `observe` / `paper` / `live`，默认 paper |
| `EXCHANGES` | 启用的交易所名称，自动两两组合 |
| `MAX_NOTIONAL` | 每腿最大名义金额，USD 估值 |
| `MAX_POSITIONS` | 全进程最大占用标的数量，包括冻结标的 |
| `ENTRY_BPS` | 扣除预算费用后的入场带宽 |
| `MIDLINE_BPS` | 按配置中的交易所先后顺序，第一所相对第二所的长期溢价中枢 |
| `SLIPPAGE_BPS` | IOC 价格保护和计算时的滑点预算 |
| `TAKE_FRACTION` | 可用深度参与比例 |
| `EXIT_PROFIT` / `STOP_LOSS` | 预计净平仓 PnL 的止盈值/亏损绝对值，USD |
| `MAX_AGE` | 行情最大允许年龄，秒 |
| `SIGNAL_SECONDS` | 同方向机会至少持续的秒数 |
| `ORDERS_PER_MINUTE` | 每个交易所跨进程共享的滑动窗口订单限额 |
| `GATE_FEE` / `HL_FEE` | 账户实际 taker 费率，使用小数比率而非 bps |
| `REDIS_URL` / `NAMESPACE` | 跨进程协调存储及命名空间 |

完整配置见 [.env.example](.env.example)。实盘还需 `GATE_KEY`、`GATE_SECRET`、`HL_KEY` 和 `HL_ACCOUNT`。HL 私钥可使用 agent 钱包，账户地址填写主账户；本版不支持 vault/subaccount 路由。Gate 需要单向持仓模式。不要与手动交易、其他机器人共用正在交易的标的，以免账户对账检测到外部敞口而冻结。

## 策略及 PnL 口径

参考 [entropy-arb 的信号与执行说明](https://github.com/your-quantguy/entropy-arb/blob/main/README.zh-CN.md)，采用净价差、可配置溢价中枢、IOC 双腿执行和不平衡减仓，未复制其源码。

入场使用实际深度累计的买入金额与卖出金额，扣除两边预估往返 taker 手续费及滑点预算。中枢只是测量参数；默认零不代表真实市场没有长期基差，策略不保证价差收敛。

```text
预计平仓 PnL
= 累计成交现金流（卖出收入 − 买入支出 − 已计费用）
+ 平多仓可获得的买盘成交收入
− 平空仓所需的卖盘成交支出
− 预估平仓手续费
```

盘口不足、行情过期或交叉盘口时不输出可执行 PnL。Redis `pnl` 数据明确标记 `estimate=true`、`funding_included=false`。本版成交费用主要使用配置费率估计，**未计入资金费、强平费及实际汇兑损益**；因此是交易价差的净平仓估值，不是交易所最终账户账单。paper 也不模拟资金费、排队、网络竞争或自身交易对后续公共盘口的冲击。

## 多进程、崩溃与恢复

三个交易所 A/B/C 会启动 AB、AC、BC 三个 `spawn` 子进程，每进程各自使用 asyncio。所有实例对同一基础币共用一把 Redis 归属锁，持仓期间不能被另一个组合操作。全局钱包 nonce 防止同一 Hyperliquid agent 多进程签名发生冲突。

锁**没有自动过期**。网络超时不等于未成交，进程退出也不等于仓位已平。系统会持久化 owner、订单 intent、已知成交和仓位；结果不确定时保持 `blocked`，不重发原订单、不自动解锁、不自动接管旧进程仓位。

遇到冻结或进程中断：先停止相关实例，通过 `status` 查看归属和日志，再在交易所按客户端订单 ID 查证成交及实际仓位。确认所有订单已终止、双边均已平仓之后，才可由运维清理该基础币的归属。当前版本不提供自动跨重启恢复交易或一键删除锁，避免把存活但暂时失联的进程和已崩溃的进程混为一谈。

Redis 必须保持同一命名空间、持久化和 `noeviction`。示例 Compose 使用 AOF `appendfsync always`，以持久性换取额外延迟。删除 Redis 数据卷、换命名空间或多个独立 Redis 连接同一账户，会破坏全局排他性。Ctrl+C 停止进程但不自动平仓；旧持仓仍需查证处理。

## 扩展交易所

继承 `src/exchange/base.py` 中的 `Exchange`，实现规则获取、连接、深度订阅、WSS 下单、WSS 查单、活动订单及实际仓位查询。在 `src/exchange/__init__.py` 的 `EXCHANGES` 注册名称，加入 `EXCHANGES` 配置即可生成新组合。

必须统一数量/价格、遵守 `Order.reduce_only`、返回真实最终成交并区分 UNKNOWN。新交易所的特殊校验覆盖 `validate_order` 并调用基类校验。当前统一模型支持线性永续；反向合约、不同指数定义、同名不同资产不能仅靠字符串匹配接入。多组合共享同一个中枢参数时，应确保适用；需要不同中枢可使用相同 Redis 命名空间启动不同交易所组合配置。

## 项目结构

```text
PySpreadBot/
├── src/
│   ├── main.py              # CLI 与两两组合多进程入口
│   ├── __main__.py          # python -m pyspreadbot 兼容入口
│   ├── config.py            # .env 配置与启动校验
│   ├── models.py            # Instrument / Order / Fill / Book / Position
│   ├── normalization.py     # 标的别名和单位映射
│   ├── cache.py             # 合约规则原子缓存
│   ├── transport.py         # WSS 重连、响应关联、推送分发
│   ├── coordination.py      # Redis 归属、日志、nonce、订单预算
│   ├── strategy.py          # 深度价差、共同步长、平仓 PnL
│   ├── execution.py         # 双腿状态机、补偿、对账
│   ├── engine.py            # 组合运行循环和持仓维护
│   └── exchange/
│       ├── __init__.py      # 适配器注册表
│       ├── base.py          # 统一交易所抽象
│       ├── gate.py
│       └── hyperliquid.py
├── tests/                   # 单位、协议、执行、并发回归测试
├── docs/architecture.md     # 设计约束和扩展约定
├── compose.yaml             # 本地持久化 Redis
├── .env                     # 本地配置/密钥，不提交
├── .env.example
├── pyproject.toml
├── setup.cfg                # egg-info 输出到根目录
├── data/contracts/          # 合约缓存，不提交
├── pyspreadbot.egg-info/     # 打包元数据，不提交
├── build/                   # 临时构建目录，不提交
└── dist/                    # wheel 和源码包，不提交
```
