# 文件与职责地图

| 文件或目录 | 主要职责 | 由谁调用 | 核心 | 风险 | 备注 |
| --- | --- | --- | --- | --- | --- |
| `app.py` | 数据抓取、计算、缓存、AI、Excel、HTTP、内嵌前端 | `启动.bat`、`agent.py`、测试 | 是 | 高 | 真实 Web 入口；修改前先定向查函数 |
| `agent.py` | 命令行 AI 交互薄包装 | `启动AI助手.bat` | 否 | 中 | 复用 `app.agent_run` |
| `monitor.py` | 本地盯盘 CLI 薄入口 | `启动盯盘.bat` | 是 | 中 | 不启动远程服务 |
| `monitoring/config.py` | 环境配置、保留和刷新期限 | 盯盘 CLI/服务 | 是 | 中 | 不回显通知凭据 |
| `monitoring/db.py` | SQLite schema 和仓储 | 盯盘/AI 草案 | 是 | 高 | 不删除用户真实数据库 |
| `monitoring/presets.py` | 三线盯盘和自动异动的安全预设 | `monitoring/cli.py` | 是 | 中 | 普通用户主入口；高级规则不在此删除 |
| `monitoring/rules.py` | 指标白名单和确定性规则引擎 | `MonitorService` | 是 | 高 | 修改触发语义需回放测试 |
| `monitoring/data.py` | 复用 `app.py` 行情并压缩每日指标 | `MonitorService` | 是 | 中 | 不保存完整图表序列 |
| `monitoring/service.py` | 交易时段、刷新、触发和通知编排 | `monitor.py watch` | 是 | 高 | 分钟循环不得调用 LLM |
| `monitoring/web.py` | Web 脱敏总览、三线设置、持仓扫描/报告编排、提醒预览和后台启停 | `app.Handler` | 是 | 中 | 模拟预览不得取行情、写事件、发通知或调用模型 |
| `monitoring/notifications.py` | 控制台、企微、Server酱适配 | `MonitorService` | 是 | 中 | 凭据只读环境变量 |
| `monitoring/assistant.py` | 按需规则草案、验证和缓存 | `suggest-rules` | 否 | 中 | 草案不自动启用 |
| `monitoring/explanations.py` | 按需事件复核、严格 JSON、缓存和用量统计 | Web 盯盘 | 否 | 中 | 只读事件和逻辑卡，不进入分钟轮询 |
| `monitoring/holding_ocr.py` | 千问持仓截图识别、严格校验、错误脱敏和缓存 | Web 持仓页 | 否 | 中 | 原图和凭据不落库；不得进入分钟轮询 |
| `monitoring/portfolio_analysis.py` | 共同截止日、趋势/相对强弱、贡献、集中度、相关性、强弱分类和历史诊断重放 | `monitoring/portfolio.py` | 是 | 高 | 诊断重放不是收益回测；质量不足必须显式降级 |
| `monitoring/portfolio.py` | 组合快照、事实证据目录、0 Token 扫描、两阶段 DeepSeek/GPT 报告、用量、缓存和最近 7 次历史 | Web 持仓页 | 是 | 高 | 第一轮不得接收用户判断；已确认事实只能引用有效 evidence_id |
| `monitoring/trading_calendar.py` | 本地交易日历读取和交易日判断 | `MonitorService`、Web 状态 | 是 | 中 | 无权威文件时明确使用工作日兜底 |
| `启动.bat` | 检查 Python、Excel 导出与筹码备用源依赖并启动 `app.py` | 用户双击 | 否 | 中 | 兼容入口，不轻易改名 |
| `启动AI助手.bat` | 临时读取 DeepSeek Key 并启动 CLI | 用户双击 | 否 | 中 | 不把输入写入文件 |
| `启动盯盘.bat` | 持续运行本地盯盘 CLI | 用户双击 | 是 | 中 | 窗口关闭即停止 |
| `requirements.txt` | Excel 导出和筹码免费备用数据源依赖 | 开发者/pip | 否 | 低 | 核心 Web 其余部分为标准库 |
| `data/industry_boards.json` | 行业板块重点排序预留配置 | 当前未参与运行时排序 | 否 | 低 | 东财动态全量结果不筛选；不是运行时快照 |
| `tests/fixtures.py` | 固定 K 线、估值和标的元数据 | `test_analysis.py` | 否 | 低 | 不请求真实接口 |
| `tests/test_analysis.py` | 分位口径、完整分析、当日分时、震荡区间和筹码估算契约 | unittest | 是 | 低 | 固定数据，不请求真实接口 |
| `tests/test_http.py` | 随机端口 HTTP/API、按需关键位和内嵌网页契约测试 | unittest | 是 | 低 | Mock 业务与模型调用 |
| `tests/test_industry_flow.py` | 行业资金流解析、缓存快照降级与市场概览兼容契约 | unittest | 否 | 低 | 使用临时快照和 Mock，不联网 |
| `tests/test_monitoring_presets.py` | 三线构建、更新隔离和 CLI 总览 | unittest | 是 | 低 | 使用临时数据库 |
| `tests/test_monitoring_web.py` | Web 后台启停和凭据脱敏 | unittest | 是 | 低 | 不请求真实行情 |
| `tests/test_monitoring_explanations.py` | 事件复核缓存、严格 JSON 和用量回滚 | unittest | 是 | 低 | 使用假模型 |
| `tests/test_holding_ocr.py` | 图片/地址校验、千问请求契约、严格 JSON 和 7 天缓存 | unittest | 是 | 低 | 使用生成图片和假模型，不联网 |
| `tests/test_portfolio_analysis.py` | 组合曲线/贡献、共同截止日、质量门控、相关性/暴露覆盖和无前视诊断重放 | unittest | 是 | 低 | 使用固定日线，不联网 |
| `tests/test_portfolio.py` | 快照、观点隔离、事实 ID 绑定、DeepSeek/GPT 契约、两次原子预留、失败实结算、缓存和 7 次历史 | unittest | 是 | 低 | 使用固定分析结果和假模型 |
| `AGENTS.md` | 后续智能体首读规则 | 维护者/智能体 | 否 | 低 | 保持精简 |
| `README.md` | 使用、安装、配置、启动与常见问题 | 用户/开发者 | 否 | 低 | 不复制详细架构 |
| `docs/ARCHITECTURE.md` | 稳定架构、数据流、接口和口径 | 开发者/智能体 | 是 | 中 | 架构变化时更新 |
| `docs/PROJECT_CONTEXT.md` | 产品边界、场景和功能建议 | 产品/开发者 | 否 | 低 | 未确认背景明确标注 |
| `docs/CURRENT_STATUS.md` | 当前进度、测试和下一步 | 后续接手者 | 否 | 低 | 控制短篇幅 |
| `docs/DECISIONS.md` | 关键兼容性与保留决策 | 开发者/智能体 | 否 | 中 | 不虚构历史原因 |
| `docs/TESTING.md` | 测试分层、命令和未覆盖风险 | 开发者/智能体 | 是 | 低 | 测试变化时更新 |
| `docs/MONITORING.md` | 盯盘规则、微信、成本和保留策略 | 用户/开发者 | 是 | 低 | 盯盘首读 |
| `docs/REFACTOR_PLAN.md` | 本轮审计和保守整理边界 | 开发者/用户 | 否 | 低 | 包含删除映射与待确认项 |
| `.codex/` | 本地 Codex 环境元数据 | Codex 桌面环境 | 否 | 低 | 已忽略，不是业务源代码 |

## `app.py` 定向阅读索引

| 区域 | 函数/对象 |
| --- | --- |
| 外部请求 | `http_text`、`fetch_json`、`api_post` |
| 标的/行情 | `_guess`、`resolve`、`_parse_qt`、`fetch_kline`、`fetch_quote`、`fetch_intraday`、`build_intraday_comparison`、`resolve_index_reference`、`fetch_eastmoney_chip_kline`、`market_overview`、`market_history` |
| 估值/财务/概念/ETF 披露/资金 | `fetch_valuation`、`fetch_industry`、`fetch_fundamentals`、`fetch_company_context`、`fetch_etf_context`、`fetch_moneyflow` |
| 纯计算 | `sma`、`ema`、`rsi`、`macd`、`boll`、`percentile_rank`、`stat_block`、`detect_consolidation_box`、`estimate_chip_distribution` |
| 综合分析 | `analyze`、`build_report`、`build_alerts`、`analyze_cached`、`build_key_levels`、`key_levels_cached` |
| Excel | `build_excel` |
| AI | `AGENT_TOOLS`、`agent_run`、`build_security_ai_evidence`、`generate_security_ai_report`、`generate_market_ai_report`、`panel_analyze`；`analyze_multidim` 仅旧后端兼容 |
| 前端 | `HTML`；今日分时/相对强弱、按需关键位/筹码、公司定位与 ETF 定位/指数追踪渲染 |
| HTTP/启动 | `Handler`（含 `GET /api/intraday`、`GET /api/key-levels` 和按需 `POST /api/security_report`）、`if __name__ == "__main__"` |
