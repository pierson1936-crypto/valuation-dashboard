# 估值分析台项目交接

这是一个只在本机运行的 A 股、ETF 和指数研究分析台，覆盖行情、估值分位、技术与资金、组合扫描、本地盯盘和可选 AI，不连接券商也不自动交易。后端是 Python 标准库 `ThreadingHTTPServer`，原生 HTML/CSS/JavaScript 与 ECharts 前端内嵌在 `app.py`，盯盘和持仓状态单独保存在本地 SQLite。安装 `requirements.txt` 后运行 `python -X utf8 app.py`，浏览器访问 `http://127.0.0.1:8688/`。

> 交接基准：2026-08-20。开发史、分支状态和窗口贡献以 Git 为唯一真相源；文档中的测试结果明确区分“本次执行”和“历史记录”。

## 1. 项目定位与技术栈

### 1.1 产品边界

- 面向本机研究使用，支持股票、ETF、指数、大盘、自选分组、持仓和规则盯盘。
- 基础查询不需要模型 Key；AI 对话、单标的/大盘报告、持仓报告、规则草案、事件复核和截图识别均为用户手工触发。
- 分钟盯盘只运行确定性规则，Token 消耗为 0；系统不接券商、不自动下单、不预测收益，输出不构成投资建议。
- Web 核心只用进程内缓存；SQLite 只属于独立盯盘/持仓模块，不得扩散到 Web 核心。

### 1.2 技术栈与外部依赖

| 层 | 当前实现 |
| --- | --- |
| 后端 | Python 3，标准库 `ThreadingHTTPServer`、`urllib`、`ThreadPoolExecutor`，只绑定 `127.0.0.1:8688` |
| 前端 | 原生 HTML/CSS/JavaScript，全部位于 `app.py` 的 `HTML` 字符串；无前端构建步骤 |
| 图表/图标/动效 | 运行时从 jsDelivr 加载 ECharts 5、Lucide 0.468.0、GSAP 3.12.5 |
| Python 依赖 | `openpyxl==3.1.5`、`baostock==0.8.9`、`mini-racer==0.14.1` |
| 数据存储 | Web 核心无数据库；盯盘/持仓使用忽略文件 `data/monitor.db` |
| 测试 | 标准库 `unittest`、Mock、固定样例、随机本机端口和临时 SQLite |
| 行情与资料 | 腾讯、东方财富、同花顺、新浪、Baostock 等公开接口；均无服务等级保证 |
| 可选模型 | DeepSeek、OpenAI；千问用于截图观察/持仓识别；Anthropic 兼容调用仍保留在代码中 |

`AGENTS.md` 中“唯一直接 Python 依赖是 openpyxl”已经过时，接手时必须以当前 `requirements.txt` 的三个固定依赖为准。`mini-racer` 只执行仓库内固定版本的同花顺签名脚本，来源和许可见 `vendor/`。

### 1.3 安装、启动和测试

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

python -X utf8 app.py
# 浏览器访问 http://127.0.0.1:8688/

$env:DEEPSEEK_API_KEY = Read-Host "请输入 DeepSeek API Key"
python -X utf8 agent.py

python -X utf8 monitor.py watch

python -X utf8 -m unittest discover -s tests -v
python -m compileall -q app.py agent.py monitor.py monitoring tests
```

普通用户可双击 `启动.bat`、`启动AI助手.bat`、`启动盯盘.bat`。网页入口会优先使用 Microsoft Edge；关闭服务窗口或按 `Ctrl+C` 会停止本机服务和依附其上的 Web 后台盯盘。

## 2. 远端仓库与分支策略

### 2.1 远端与克隆

- 远端名：`origin`
- Fetch/Push 地址：`https://github.com/pierson1936-crypto/valuation-dashboard.git`
- 默认远端分支：`origin/main`
- 唯一标签：带注释标签 `v0.2.0`，标签对象 `3533192`，指向 `4cb4441`
- 2026-08-20 已用在线 `git ls-remote` 核对远端，不只是读取本地缓存的 `origin/*`。

接手当前综合开发线：

```powershell
git clone https://github.com/pierson1936-crypto/valuation-dashboard.git
cd valuation-dashboard
git switch --track origin/codex/market-quadrant-kline-structure-mvp
git pull --ff-only
git status --short --branch
git log --oneline -20
```

查看稳定基线或体验包分支：

```powershell
git switch main
git show v0.2.0

git switch --track origin/codex/experience-package
```

现有克隆更新：

```powershell
git fetch origin --prune
git switch codex/market-quadrant-kline-structure-mvp
git pull --ff-only
git rev-list --left-right --count 'HEAD...@{upstream}'
```

PowerShell 会解释未加引号的 `@{upstream}`，所以上述修订范围必须整体加单引号。

### 2.2 分支职责

- `main`：已发布/可回退基线，只接受经过确认、回归和验收的整合结果；当前仍停在 `v0.2.0@4cb4441`。
- `feat/*`、`feature/*`、`codex/*`：单一功能或短期修复分支。修改前先确认起点，完成后定向测试、真实 UI 验收（如涉及 UI）、小步提交，再并入综合线。
- `codex/market-quadrant-kline-structure-mvp`：当前源代码综合开发线。分支名是历史遗留，当前大盘已经不是四象限散点图，不能按名称判断页面现状。
- `codex/experience-package`：从综合线功能提交 `f845341` 分出，增加 Windows 体验包脚本、快速开始和启动器调整；它不是 `main` 发布版。
- 提交前只暂存明确文件，禁止 `git add .`；不在功能分支顺手改金融口径、数据库状态或无关文档。

### 2.3 全部分支清单

本次审计共整理 `13` 个本地分支、`5` 个实际远端分支引用和 `2` 个工作树。创建本交接提交之前共有 `24` 个可达提交；没有 stash，也没有不可达提交对象。

| 本地分支 | 审计时 tip / 远端 | 用途与包含关系 | 建议 |
| --- | --- | --- | --- |
| `main` | `4cb4441` / `origin/main` | `v0.2.0` 稳定基线 | 保留；未经发布确认不要直接推进 |
| `feat/key-level-view-toggle` | `2f48467` / 同名远端 | K 线/筹码双视图；已被综合线包含 | 主线发布后可删本地/远端历史分支 |
| `feature/industry-fund-flow` | `97ebf26` / 同名远端 | 行业资金主备源；已被综合线包含 | 主线发布后可删本地/远端历史分支 |
| `codex/market-quadrant-kline-structure-mvp` | 功能 tip `f845341`；本文件所在提交在其上 / 同名远端 | 当前综合开发线 | 必须保留；本交接提交推送到这里 |
| `codex/kline-presentation-hierarchy` | `05f5ba4` / 无 | 简化 K 线主图标注；经 `7cfcab6` 合并 | 可清理的本地审阅锚点 |
| `codex/kline-structure-usability` | `8556439` / 无 | K 线结构与筹码请求解耦；经 `b29d752` 合并 | 可清理的本地审阅锚点 |
| `codex/intraday-volume-focus-watchlist` | `a44c5d8` / 无 | 实际提交只有重点自选高亮；经 `7b783d6` 合并 | 可清理；分支名中的分时量比未实现 |
| `codex/market-flow-tab-animation` | `0b19f8e` / 无 | 切页重播、起始帧和重点行语义测试 | 可清理的本地审阅锚点 |
| `codex/market-refresh-visible` | `a07dccc` / 无 | 切回大盘强制刷新和验收记录 | 可清理的本地审阅锚点 |
| `codex/market-refresh-queue` | `cc16e15` / 无 | 活跃请求期间的强制刷新排队 | 可清理的本地审阅锚点 |
| `codex/market-flow-clock-fix` | `d5479bb` / 无 | 统一动画时钟 | 可清理的本地审阅锚点 |
| `codex/market-flow-immediate-replay` | `f845341` / 无 | 先重播快照、再等待刷新；与功能 tip 同点 | 可清理的本地别名分支 |
| `codex/experience-package` | `2c952d5` / 同名远端 | Windows 体验包构建与快速开始 | 暂时保留；全新 Windows 首次运行仍待验收 |

远端实际分支为 `main`、`feat/key-level-view-toggle`、`feature/industry-fund-flow`、`codex/market-quadrant-kline-structure-mvp`、`codex/experience-package`。不要立即删除分支：Git 无法判断 GitHub 是否仍有开放 PR；应先确定 v0.3.0 的最终整合线、合入 `main`、打标签并确认远端可回退，再清理已包含的短分支。本次没有执行分支删除、重命名或合并。

## 3. 目录结构与关键文件

### 3.1 当前综合线目录树

```text
.
├─ AGENTS.md                         # AI/维护者接手规则
├─ HANDOFF.md                        # 本文件，Git/分支/状态总入口
├─ README.md                         # 用户安装、配置和使用
├─ CHANGELOG.md                      # 早期版本记录；8 月 14 日后明显滞后
├─ app.py                            # Web 核心、接口、计算、AI、Excel、内嵌前端
├─ agent.py                          # CLI AI 薄入口
├─ monitor.py                        # 盯盘 CLI 薄入口
├─ requirements.txt                 # 三个固定 Python 依赖
├─ 启动.bat
├─ 启动AI助手.bat
├─ 启动盯盘.bat
├─ monitoring/
│  ├─ config.py                     # 环境配置、保留期、刷新期
│  ├─ db.py                         # SQLite schema 与仓储
│  ├─ presets.py                    # 三线和自动异动预设
│  ├─ rules.py                      # 确定性规则引擎
│  ├─ data.py                       # 复用 app.py 行情并压缩指标
│  ├─ service.py                    # 交易时段、轮询、触发、清理
│  ├─ web.py                        # Web 盯盘/持仓控制器
│  ├─ cli.py                        # monitor.py 的命令实现
│  ├─ notifications.py              # 控制台、企微、Server酱
│  ├─ trading_calendar.py           # 本地交易日历与工作日兜底
│  ├─ assistant.py                  # 按需规则草案
│  ├─ explanations.py               # 按需事件复核
│  ├─ holding_ocr.py                # 千问截图识别、清洗、脱敏、缓存
│  ├─ portfolio.py                  # 组合快照、两阶段 AI、缓存与历史
│  └─ portfolio_analysis.py         # 确定性组合指标与历史诊断重放
├─ tests/
│  ├─ fixtures.py
│  ├─ test_analysis.py
│  ├─ test_http.py
│  ├─ test_industry_flow.py
│  ├─ test_monitoring_db.py
│  ├─ test_monitoring_rules.py
│  ├─ test_monitoring_service.py
│  ├─ test_monitoring_presets.py
│  ├─ test_monitoring_web.py
│  ├─ test_monitoring_assistant.py
│  ├─ test_monitoring_explanations.py
│  ├─ test_holding_ocr.py
│  ├─ test_portfolio.py
│  └─ test_portfolio_analysis.py
├─ docs/
│  ├─ CURRENT_STATUS.md             # 最新实现、验证、待办
│  ├─ PROJECT_CONTEXT.md            # 产品边界与场景
│  ├─ ARCHITECTURE.md               # 数据流、缓存、接口、口径
│  ├─ FILE_MAP.md                   # 文件职责与 app.py 函数索引
│  ├─ MONITORING.md                 # 盯盘/持仓/通知完整说明
│  ├─ TESTING.md                    # 测试分层与历史验收
│  ├─ DECISIONS.md                  # 不可随意推翻的技术决策
│  ├─ REFACTOR_PLAN.md              # 早期审计计划，不是当前路线图
│  └─ examples/trading_calendar.json
├─ data/
│  ├─ .gitkeep
│  ├─ industry_boards.json          # 预留配置，当前不参与动态排序
│  ├─ monitor.db                    # 忽略的真实用户状态，严禁测试/提交
│  ├─ industry_flow_snapshot.json   # 忽略的运行时快照
│  └─ trading_calendar.json         # 可选本地权威日历；仓库默认无此文件
└─ vendor/
   ├─ README.md
   ├─ AKSHARE_LICENSE
   └─ akshare_ths.js                # 固定版本的同花顺签名脚本
```

`codex/experience-package@2c952d5` 额外包含 `scripts/build_experience_package.ps1` 和 `快速开始.txt`，并修改 `.gitignore`、`启动.bat`、`docs/CURRENT_STATUS.md`、`docs/TESTING.md`。这些文件不在当前综合线的功能 tip `f845341` 中。

### 3.2 新 AI 必须重点扫描的文件

| 任务 | 必读路径 | 精确职责 |
| --- | --- | --- |
| 所有任务 L1 | `AGENTS.md`、`HANDOFF.md`、`docs/CURRENT_STATUS.md` | 不可突破的边界、Git 全貌、最新实现/验证/待办 |
| 架构定位 | `docs/ARCHITECTURE.md`、`docs/FILE_MAP.md` | 数据流、缓存、API、函数入口和文件职责 |
| 历史取舍 | `docs/DECISIONS.md` | 金融口径、单文件核心、SQLite 隔离、AI 观点隔离等决策 |
| Web/行情/UI | `app.py` | 唯一真实 Web 入口；先用 `rg` 定位函数，禁止从头通读大文件 |
| 盯盘/持仓 | `docs/MONITORING.md`、`monitoring/config.py`、`db.py`、`presets.py`、`rules.py`、`data.py`、`service.py`、`web.py` | 配置、仓储、三线、轮询、触发、Web 控制 |
| 通知/日历 | `monitoring/notifications.py`、`monitoring/trading_calendar.py` | 外部通知和交易日判断 |
| 截图识别 | `monitoring/holding_ocr.py`、`tests/test_holding_ocr.py` | 图片上传、千问请求、严格清洗、错误脱敏、7 天缓存 |
| 组合分析 | `monitoring/portfolio_analysis.py`、`monitoring/portfolio.py`、对应两份测试 | 确定性组合指标、无前视诊断、两阶段 DeepSeek/GPT 报告 |
| 行业资金 | `vendor/README.md`、`vendor/akshare_ths.js`、`tests/test_industry_flow.py` | 固定签名脚本、来源许可、主备源与快照契约 |
| 测试/验收 | `docs/TESTING.md` 和本次涉及的 `tests/test_*.py` | Mock 边界、固定样例、随机端口、浏览器验收要求 |
| 安装/兼容入口 | `requirements.txt`、`README.md`、三个批处理文件 | 实际依赖、用户入口和环境配置 |

修改 `app.py` 前只定向查本次相关函数。常用入口为：

- 标的与数据：`resolve`、`fetch_kline`、`fetch_intraday`、`fetch_valuation`、`fetch_fundamentals`、`fetch_company_context`、`fetch_etf_context`、`fetch_moneyflow`、`fetch_industry_boards_*`。
- 计算与编排：`percentile_rank`、`detect_consolidation_box`、`estimate_chip_distribution`、`analyze`、`analyze_cached`、`build_key_levels`、`key_levels_cached`、`market_overview`、`market_history`。
- AI 与导出：`build_security_ai_evidence`、`generate_security_ai_report`、`generate_market_ai_report`、`agent_run`、`panel_analyze`、`build_excel`。
- HTTP/前端：`Handler`、`LocalThreadingHTTPServer`、`HTML`。

## 4. 开发历程复盘

### 4.1 如何理解“多个窗口”

Git 没有聊天窗口 ID。下表把一个分支或一组连续短分支视为一个可审计工作流；只有提交标题明确写出的 `[workbuddy]` / `[codex]` 或分支名可以作为工具归属线索，不能据此虚构具体对话。`a37f5a7` 和 `4cb4441` 都是大体量汇总提交，Git 无法继续拆出它们内部每个旧窗口的贡献。

| 日期/工作流 | 起点与关键提交 | Git 可证明的工作 | 状态 |
| --- | --- | --- | --- |
| 2026-07-30 初始导入 | 根提交 `a37f5a7` | 一次性加入 Web/AI/盯盘入口、`monitoring/`、测试和文档 | 已完成；更早过程无法由当前 Git 细分 |
| 2026-08-13 v0.2.0 基线 | `a37f5a7` -> `4cb4441` | 大幅扩展持仓 OCR、组合确定性分析、两阶段报告、ETF/分时/关键位和文档；打 `v0.2.0` | 已发布基线；内部多窗口贡献不可再分 |
| K 线/筹码双视图 | `4cb4441` -> `adf024b` -> `2f48467` | 双视图互斥、重复点击恢复、ETF 隐藏筹码；箱体外波段回退、Baostock 备用源和失败重试 | 已完成并入综合线；独立分支可归档 |
| 行业资金流 | `2f48467` -> `20f6201 [workbuddy]` -> `97ebf26` | 首版东财行业资金与动效；后因东财不稳定改为同花顺主源、东财完整榜回退、最近快照兜底 | 已完成并入综合线；真实源仍可能波动 |
| 资金结构/价格行为 | `97ebf26` -> `6ef5cae` -> `e8a73ac` | 首版四象限和价格结构；随后以净流入/流出 TOP 8 对称柱和四类环图替代四象限 | 价格结构保留；四象限视觉已被替代 |
| 交接文档同步 | `e8a73ac` -> `d6c7cf1` | 更新当时的项目状态和上下文 | 已完成，但后续提交使部分状态文字过时 |
| K 线呈现层 | `d6c7cf1` -> `05f5ba4`，合并 `7cfcab6` | 移除主图 HH/HL/LH/LL 英文点位和文字，把中文结论移至图下 | 已完成并入综合线 |
| K 线请求可用性 | `7cfcab6` -> `8556439`，合并 `b29d752` | K 线结构请求不再等待东财/Baostock 筹码；拆分 `include_chip` 缓存和接口 | 已完成并入综合线 |
| 重点自选 | `b29d752` -> `a44c5d8`，合并 `7b783d6` | 星标重点行和仅切换视觉突出状态 | 已完成；不改变分组、盯盘、风险或 SQLite |
| 大盘重播/刷新修复链 | `11209a7`、`4ad96c7`、`0b19f8e`、`ee12147`、`a07dccc`、`cc16e15`、`d5479bb`、`f845341` | 切页重播、保留起始帧、每次切回刷新、请求中排队、统一 `performance.now()`、先重播完整快照再取新数据 | 已完成并入综合线；动画节奏仍需肉眼验收 |
| Windows 体验包 | `f845341` -> `2c952d5` | 构建脚本、快速开始、启动器安装依赖、ZIP 安全清单 | 单独分支已推送；全新 Windows 首次安装/打开仍待验收 |

### 4.2 模块演化与边界

**入口、分析和 Web**

- `a37f5a7` 已包含 `resolve -> fetch_* -> analyze -> percentile/technical/report -> analyze_cached -> Handler -> HTML` 主链、Excel、AI 对话和多股对比。
- `4cb4441` 汇总加入单标的编号事实 AI、大盘开放式 AI、ETF 上下文、分时相对强弱、按需关键位和持仓能力。
- 旧多维分析页面入口已下线，`POST /api/multidim` 仅兼容保留；买一/卖一和布林带展示已撤下，部分底层计算/字段仍保留兼容。

**盯盘、持仓和 AI**

- 盯盘核心从初始提交开始就采用本地 SQLite、确定性规则、连续确认、冷却、回差、重新武装和通知重试；分钟循环不得调用 LLM。
- `4cb4441` 新增截图 OCR、共同截止日组合计算、集中度/相关性、无前视历史诊断和两阶段观点隔离报告。
- 代码和 Mock 回归较完整，但真实报告不能标记为成功：历史本地库保留 6 条截断失败记录；关闭 DeepSeek thinking 后尚未完成真实成功复验，GPT 真实 Key 也未验证。

**ETF、分时、K 线和筹码**

- ETF 上下文为定期披露，不是实时仓位；只要行业配置或持仓存在即可展示，跟踪指数是补充，不得写固定代码白名单。
- `/api/intraday` 独立加载当日分钟行情和参考指数相对强弱，失败不阻塞主分析；真实分钟接口与 ETF 名称匹配仍待复验。
- K 线 UI 默认请求 `/api/key-levels?code=...`，不抓筹码；筹码按钮显式带 `chip=1`。但用户手工生成单标的 AI 报告仍会以 `include_chip=True` 尝试取得受限筹码证据，不能笼统写成“全系统只有筹码按钮触发筹码源”。
- 筹码是近 120 日的本地估算，不是账户持仓或客户端成品；只能交叉验证价格结构，不进入风险评分、盯盘或买卖建议。
- “截至同一分钟累计量比”只有调查结论，没有实现提交：腾讯历史分钟接口当时 DNS 不可达、真实字段未完成固定样例核验，因此没有接入页面。

**行业资金与 UI**

- `main_net` 始终只表示东方财富主力净流入；同花顺实际净额使用 `flow_net/flow_kind/flow_source/flow_label`，不能混写口径。
- 同花顺、东方财富都失败时保留最近完整快照；不完整单边快照不得进入资金流排名或 AI 事实。
- 重点自选最初实现曾存在“取消突出会隐藏重点行”的风险，后续代码和测试锁定为“保留全部行，仅关闭高亮”。
- 大盘动画修复链说明一个历史教训：RAF 回调时间和 `Date.now()`/`performance.now()` 混用会直接跳到终态；切回页面时应先重播本地完整快照，再并行刷新。

## 5. 当前状态与待办

### 5.1 已实现

- 股票 PE-TTM/PB/价格近五年目标窗口分位、技术指标、财务、资金流、风险报告和 Excel；ETF/指数只做价格分位。
- 同花顺行业资金净额主源、东方财富完整结果回退、最近完整快照，大盘对称柱/环图和切页刷新动效。
- 当日分时与参考指数相对强弱，K 线结构/筹码互斥视图，K 线结构与筹码请求解耦。
- 自选本地分组、组均/中位涨幅、上涨家数、当日轨迹、重点星标。
- 三线盯盘、确定性规则、事件、通知重试、持仓逻辑卡、规则草案和事件复核。
- 千问截图识别与人工校对、0 Token 组合扫描、历史诊断、可选两阶段 DeepSeek/GPT 报告。
- `app.py`、`agent.py`、`monitor.py` 和三个批处理兼容入口均保留。

### 5.2 本次交接实际验证

- 在线核对远端地址、5 个远端分支和 `v0.2.0` 标签；检查 13 个本地分支、全部提交、2 个工作树、stash、reflog 和不可达对象。
- 两个工作树的已跟踪文件在写本文前均干净；所有提交均可从在线远端分支到达，没有只存在本机 Git 对象库的提交。
- 运行 `python -X utf8 -m unittest discover -s tests -v`：共 192 项，191 项通过，1 项错误。
- 唯一错误：`tests.test_portfolio.PortfolioReportTests.test_first_stage_cannot_see_locked_user_judgment_and_cache_is_reused`。测试以固定 `2026-08-04` 创建 7 天报告，假模型回调却调用未传同一 `now` 的 `get_latest_portfolio_report()`；在当前日期查询时记录已过期并返回 `None`。这是时间依赖测试债，本次交接没有修改业务代码或测试。
- 本次没有启动/重启 8688，也没有做新的浏览器验收，因为只改交接文档；不要把下列历史浏览器记录当成本次检查。

### 5.3 有效的历史验证记录

- 2026-08-18：行业资金图、重点突出等定向 68 项通过；`app.py` 编译、嵌入 JavaScript 和 `git diff --check` 通过；真实 8688 页面 SVG 非空、无横向溢出、控制台无错误。
- 2026-08-17：K 线呈现定向 66 项通过；完整 190 项中 189 项通过，失败同属上述持仓报告测试链。
- 2026-08-17：行业资金/分析/HTTP 定向 85 项通过；完整 188 项中 187 项通过。
- 更早的 126/126、81/81 等记录属于当时版本，不代表当前综合线全绿。

### 5.4 未提交和本地专有状态

Git 审计开始时，主工作树无已暂存或未暂存的已跟踪改动，但有 12 个未跟踪交付物，均未纳入本次提交：

- `体验包/估值分析台-体验包-20260818.zip`；ZIP 的 `VERSION.txt` 标明源提交 `2c952d5`，共 30 个条目。
- `体验包/说明手册素材/WebCodex扫描与可视化加工提示词.txt`。
- `体验包/说明手册素材/截图清单.txt`。
- `体验包/说明手册素材/原始截图/01-market-overview.png` 至 `09-analysis-help-dialog.png`，共 9 张。

这些是当前唯一明确没有进入 Git 的交付物，离开本机后可能丢失。`codex/experience-package` 已提交可重建 ZIP 的脚本，但原始截图和加工提示词并不由脚本重建；是否另存到公司批准的制品库，应由维护者决定，不要直接把生成 ZIP 和素材用 `git add .` 塞入源码仓库。

另外，`data/monitor.db` 和 `data/industry_flow_snapshot.json` 当前存在且被忽略；前者是用户真实状态，后者是运行时快照，都不属于“未提交代码”，不得读取测试、删除或提交。浏览器 `localStorage` 中的自选、分组、模型 Key 和页面偏好也不在 Git 中。

### 5.5 下一步优先级

1. 决定 v0.3.0 最终整合策略：当前综合线含本交接，体验包分支另含 `2c952d5`；先组合并完成验收，再合入 `main` 和打正式标签。`CHANGELOG.md` 虽写“v0.3.0 进行中”，当前没有 v0.3.0 标签或发布。
2. 修复持仓报告固定日期测试，使测试查询与注入的 `now` 使用同一时钟，再确认完整 192 项全绿。
3. 在独立端口和临时数据库完成最新组合扫描/报告的宽屏浏览器验收，核对共同截止日、贡献、集中度、相关性、强弱和历史诊断。
4. 修复自选报价失败覆盖：保留最近成功值，标注暂用状态/更新时间，失败样本不得写入新分组轨迹；先不要增加高频重试或多数据源轮询。
5. 真实复验分时接口、ETF 跟踪指数匹配、东财/Baostock 筹码稳定性、除权一致性和不同行情样例。
6. 仅在用户明确提供额度时验证真实 DeepSeek/GPT/千问；真实微信通知与失败恢复也需人工触发验收。
7. 修复 TLS 校验、动态 HTML 转义和浏览器 Key 风险；统一 HTTP 状态、错误结构、数据源状态和日志。
8. 使用权威交易所日期补齐本地 `data/trading_calendar.json`；手机端开发仍暂停，不要自行恢复。

## 6. 已知坑、易错点与历史技术债

### 6.1 金融与数据口径

- 不得擅改 `YEARS`、`KLINE_N`、PE-TTM/PB-MRQ 字段、分位算法、负值/空值处理、行业正值中位、前复权、风险评分、数据源或 API 字段含义。
- 分位公式是 `(小于当前值数量 + 0.5 * 等于当前值数量) / 有效样本数 * 100`；只忽略 `None`，历史 PE/PB 负值继续参与，行业中位数才只取正值。
- K 线、PE/PB 来自不同接口，起止日和样本数互不相同；“近五年”是目标上限，不保证完整五年。
- 实时报价会覆盖响应的 `price/chg`，但 `price_pct`、区间和规则报告仍基于最近日 K 收盘，这是尚未解决的口径一致性风险。
- ETF 持仓/行业配置是定期披露，不是实时仓位；历史诊断按当前持仓数量静态回看，不是包含交易、现金、费用、税费和分红的收益回测。

### 6.2 外部接口和缓存

- 免费接口会断开、限流、DNS 失败或字段漂移。东财行业流和筹码均出现过 `Remote end closed connection`，不能因一次失败擅自换口径或删除降级链。
- 自选报价当前仍会用失败空值覆盖最近成功报价，且最多 12 个并发请求会等待最慢标的。
- 进程缓存 TTL 不同：分析 180 秒、市场 120 秒、行业资金 300 秒、关键位成功 6 小时/失败 5 分钟、分时成功 60 秒/失败 15 秒、多日市场 15 分钟。修改时先查 `docs/ARCHITECTURE.md`。
- 启动服务前先检查 8688 监听者并查询实际页面；旧进程/端口冲突会让源码已更新但页面仍像旧版本。

### 6.3 安全与可维护性

- TLS 证书和主机名校验当前关闭，是高优先级安全债；动态 `innerHTML`、第三方 CDN 和浏览器 `localStorage` Key 使服务只能保持本机访问。
- 多数业务错误仍使用 HTTP 200 + JSON `error`；未处理异常文本会返回前端，日志不结构化且仍有 `DEBUG` 输出。
- `agent.py` 旧提示允许把 Key 写入源码，与现行禁令冲突；只能用环境变量或页面临时输入，绝不能提交真实 Key。
- `app.py` 同时承载抓取、计算、AI、HTTP 和前端，是大型单文件。只有直接边界测试就位后，才按数据访问、计算、Web/前端小步拆分。
- `CHANGELOG.md` 的 `feature/industry-fund-flow` 状态和 key-level 提交数已经过时；`docs/PROJECT_CONTEXT.md` 也停在较早分支状态。当前状态优先级为 Git -> `HANDOFF.md` -> `docs/CURRENT_STATUS.md` -> 专题文档。

### 6.4 交互语义

- K 线结构和筹码视图必须互斥；再次点击当前按钮恢复原始 K 线；ETF 隐藏筹码；关键位失败不能清空主分析。
- 筹码只称“本地估算成本密集区”，不能写成真实持仓、确定支撑压力或买卖信号；新 AI 证据不得包含 `cost_70` 和 `profit_ratio_pct`。
- 重点自选的“取消突出”只能移除视觉高亮，不能隐藏行、删星标、改分组或触碰 SQLite。
- 行业资金颜色、排序和数值口径不能由动效改变；过期快照只给状态提示，不应灰化有效红绿数据。
- UI 修改必须启动真实服务做宽屏/窄屏检查，包括文字清晰、无溢出、图形非空、颜色、交互和控制台；静态字符串测试不等于视觉验收。

## 7. 给接手新 AI 的启动指令

以下整段可直接复制给 WorkBuddy 或其他新 AI：

```text
你正在接手本地项目 D:\Claude-data\估值分位查询工具。

目标：在尽量少扫描的前提下继续开发，并保护既有金融口径、API 语义、真实本地状态和兼容入口。

L1 建立地图：
1. 先确认当前目录，并只读执行 git status --short --branch、git branch -a、
   git log --all --oneline --graph --decorate。
2. 按顺序精读 AGENTS.md、HANDOFF.md、docs/CURRENT_STATUS.md、
   docs/ARCHITECTURE.md、docs/FILE_MAP.md。
3. 涉及盯盘/持仓时再读 docs/MONITORING.md；涉及测试或历史取舍时再读
   docs/TESTING.md、docs/DECISIONS.md。
4. 不盲目通读全库，不展开 .git、缓存、.agents、.codex、.workbuddy，
   不读取或修改 data/monitor.db。
5. 先分开汇报：已实现、历史已验证、本轮已验证、待验证、仅建议。

L2 修改前：
1. 先运行 git log --oneline -20，理解近期演化、当前分支和直接依赖。
2. 根据 docs/FILE_MAP.md 用 rg 定位本次函数/API，只精读目标文件、直接调用者、
   直接依赖和对应 tests/test_*.py；不要从头扫描 app.py。
3. 明确拟修改文件和不修改的边界。核心口径变化必须先补固定样例，给出前后对比，
   并等待用户确认。
4. 保留 app.py、agent.py、monitor.py 和三个批处理入口；保护 data/monitor.db。

L3 修改后：
1. 先跑直接相关测试，再跑完整 unittest、compileall、内嵌 JavaScript 语法检查和
   git diff --check。
2. UI 改动必须启动真实服务，在宽屏/窄屏检查文字、溢出、图形非空、颜色、交互和
   控制台；不要用静态字符串检查冒充视觉验收。
3. 外部接口和模型测试优先使用 Mock；不得使用、记录或提交真实凭据。
4. 小步提交，先列出明确文件，禁止 git add .。
5. 修改后更新 HANDOFF.md 的“当前状态”和 docs/CURRENT_STATUS.md；架构、文件职责、
   测试或关键决策变化时，再更新对应专题文档，避免复制同一段内容。

绝对边界：不得擅自修改 YEARS、PE/PB、分位算法、负值/空值处理、前复权、风险评分、
数据源或 API 字段含义；分钟盯盘不得调用 LLM；不得删除、覆盖、测试读取或提交
data/monitor.db；不得把筹码估算、分时相对强弱或 AI 文本包装成交易建议。
```

## 8. 协作边界与禁忌

- 核心口径变更必须先写固定样例和前后对比，取得用户确认后再实现。
- 不提交 `.env`、Key、Cookie、Token、密码、日志、Excel、SQLite、行业运行时快照或浏览器配置；错误输出也不得泄露真实值。
- 测试必须使用临时数据库，不得读取、覆盖或删除 `data/monitor.db`。
- 分钟盯盘不得调用大模型；AI 功能只能显式触发、缓存、记录实际用量并受模型白名单约束。
- 不破坏 `app.py`、`agent.py`、`monitor.py` 和三个批处理兼容入口；不引入前端框架，不重写应用，不把 SQLite 引入 Web 核心。
- ETF/指数/股票逻辑必须由输入代码和公开类型驱动，禁止为验收样例写 allowlist 或硬编码结论。
- 不把概念标签当主营事实，不把实时价格、分时相对强弱、筹码估算或 AI 文字当作买卖信号。
- 不自行恢复手机端开发，不未经确认更换数据源/模型/框架，不删除历史分支或本地制品。
- 修改范围只限本次任务的文件和直接依赖；发现无关脏文件时保留并报告，不能回滚他人的工作。
- 提交前核对 `git diff --check`、暂存文件清单和工作树；发布前核对本地/远端 SHA、标签和回退点。

## 9. 离职交接快照

- 当前主要源代码综合线：`codex/market-quadrant-kline-structure-mvp`，交接前功能 tip 为 `f845341`；本文件所在提交是新的交接点。
- 稳定发布基线：`main@4cb4441` / `v0.2.0`；后续功能尚未合入 `main`，不要把 `main` 当当前功能完整版。
- Windows 体验包：`codex/experience-package@2c952d5`，已推送；它和本交接提交都从 `f845341` 继续，后续发布前需要明确整合方向。
- 全部已提交内容均有远端引用；唯一 Git 外离机风险是 12 个 `体验包/` ZIP/提示词/截图素材。
- stash 为空，两个工作树的已跟踪内容无遗漏；本次未删除任何分支或本地数据。
- 新 AI 首次进入不要推进功能，先按第 7 节 L1 复核当前分支、最新提交和工作树，再等待具体开发指令。
