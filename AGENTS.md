# 项目接手入口

## 项目与技术栈

这是一个本地运行的 A 股/ETF/指数分析台，提供行情、PE/PB/价格历史分位、
技术指标、资金流、规则报告、Excel 导出和可选 AI 分析。后端使用 Python
标准库 `ThreadingHTTPServer`，前端内嵌在 `app.py`，图表运行时从 CDN 加载
ECharts；唯一直接 Python 依赖是 Excel 导出的 `openpyxl`。Web 核心没有数据库，
独立盯盘模块使用本地 SQLite。

## 阅读顺序

1. `docs/CURRENT_STATUS.md`：当前进度、已知问题和下一步。
2. `docs/ARCHITECTURE.md`：入口、数据流、缓存和接口。
3. `docs/FILE_MAP.md`：定向找到需要修改的文件/函数。
4. 涉及盯盘时读 `docs/MONITORING.md`。
5. `docs/TESTING.md`：测试与验收方式。
6. 只打开与任务有关的源码，不要重复扫描整个仓库或展开缓存目录。

## 入口与命令

- 网页：双击 `启动.bat`，或运行 `python -X utf8 app.py`，访问
  `http://127.0.0.1:8688/`。
- CLI AI：双击 `启动AI助手.bat`，或设置 `DEEPSEEK_API_KEY` 后运行
  `python -X utf8 agent.py`。
- 本地盯盘：双击 `启动盯盘.bat`，或运行 `python -X utf8 monitor.py watch`。
- 测试：`python -X utf8 -m unittest discover -s tests -v`。

## 核心位置

- 代码识别/抓取：`resolve`、`fetch_kline`、`fetch_valuation`、
  `fetch_fundamentals`、`fetch_moneyflow`。
- 计算/编排：`percentile_rank`、技术指标函数、`analyze`、`build_report`。
- 缓存：`market_overview`、`analyze_cached`。
- HTTP/API：`Handler`；前端：`HTML`；AI：`agent_run`、`panel_analyze`。
- 盯盘：`monitoring/presets.py`、`db.py`、`rules.py`、`service.py`、
  `web.py`、`notifications.py`；
  手工 AI 草案：`monitoring/assistant.py`。

## 修改规则

- 不得擅自修改 `YEARS`、PE/PB 字段、分位算法、负值/空值处理、前复权方式、
  风险评分、数据源或 API 字段含义。
- 核心口径变更必须先写固定样例和前后对比，再取得用户确认。
- 保留 `app.py`、`agent.py` 和三个批处理兼容入口；小步拆分并逐组测试。
- 不把 Key、Cookie、Token 或密码写入代码、测试、日志和文档；不输出真实值。
- 不提交 `.env`、缓存、日志或导出的 Excel。外部 API 测试优先使用 Mock。
- 分钟盯盘不得调用大模型；AI 草案和未来事件解释必须显式启用、缓存并设置限额。
- `data/monitor.db` 是用户本地状态，不得在测试中覆盖或删除；测试使用临时数据库。
- UI 修改后必须启动真实服务并做浏览器检查；静态字符串检查不等于视觉验收。
- 修改完成后更新 `CURRENT_STATUS.md`；架构、文件职责、测试或关键决策变化时，
  分别更新对应文档，避免复制同一段内容。
