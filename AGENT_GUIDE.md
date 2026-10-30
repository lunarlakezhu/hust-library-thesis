# hust-library-thesis — Agent 使用说明

> 面向对象：**AI agent**（ZCode / Kimi Code / 其他 MCP 客户端里的编码智能体）。
> 目标：让任何 agent 在不读源码的情况下，正确、克制地用这个 MCP 完成
> 「华科图书馆 → 知网学位论文」的检索、详情、全文下载与本地阅读。
> 人类读者请看 [README.md](README.md)；本文件是 agent 视角的完整参考。

---

## 1. 这是什么

MCP 服务器 `hust-library-thesis`，数据源名 **`hust_cnki_thesis`**。复刻人工链路：
华科图书馆数字资源统一访问平台（libresource.hust.edu.cn）「智能访问」→ 知网学位论文库
（博士 CMFD + 优秀硕士 CDFD）→ 检索 → 详情 → PDF 下载 → 本地按页阅读。

- 检索/详情是公开数据；**全文下载与阅读依赖机构订阅态**（本机网络通常已被知网识别为华中科技大学）。
- 全文是真原文：与浏览器里点「PDF下载」得到的是同一个文件，不是摘要或节选。

## 2. 接入方式

### 2.1 ZCode（已注册）

`C:\Users\<用户名>\.zcode\cli\config.json` → `mcp.servers`：

```json
"hust-library-thesis": {
  "type": "stdio",
  "command": "C:\\Users\\<用户名>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
  "args": ["D:\\hust-library-thesis\\server\\hust_library_thesis_server.py"],
  "timeoutMs": 600000,
  "enabled": true
}
```

重启 ZCode 后工具名为：
`mcp__hust-library-thesis__get_data_source_desc`、`mcp__hust-library-thesis__call_data_source_tool`。

### 2.2 其他 MCP 客户端（Kimi Code / Qoder / 任意 stdio 客户端）

同一命令行注册即可（JSON `mcpServers` 通用格式）：

```json
"hust-library-thesis": {
  "command": "C:\\Users\\<用户名>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
  "args": ["D:\\hust-library-thesis\\server\\hust_library_thesis_server.py"]
}
```

依赖：Python 3.12 + `mcp`(FastMCP) + `playwright`（用 msedge channel）+ `pymupdf`，均已装好。
浏览器层跑在 `--op` 子进程里，结果经文件回传，**不污染 stdio 传输**。

**Kimi Code**（已注册，2026-10-30）：条目写在 `C:\Users\<用户名>\.kimi-code\mcp.json` 的
`mcpServers` 下，额外带 `"enabled": true` 与 `"timeoutMs": 600000`（下载大 PDF 需 1–3 分钟）。
改完须重启客户端，工具名变为
`mcp__hust-library-thesis__get_data_source_desc`、`mcp__hust-library-thesis__call_data_source_tool`。
未重启时可在 Bash 里用 `tests/test_client.py call <api> '<json>'` 直接排障。

### 2.3 配套 skill（ZCode）

`C:\Users\<用户名>\.agents\skills\hust-library-thesis\SKILL.md` —— 行为纪律（何时用、频率、
配额、失败处理）。触发词：找中文硕博学位论文 / 下知网 PDF / 读学位论文全文 / 华科图书馆 / 知网。
其他客户端没有 skill 机制时，把本文件第 4–6 节当作系统提示词补充即可。

---

## 3. 工具契约（kimi-datasource 同形，永远两步走）

```jsonc
// 第 1 步：取 API 文档（参数以文档为准；文档更新后勿凭记忆调用）
get_data_source_desc { "name": "hust_cnki_thesis" }

// 第 2 步：分发调用
call_data_source_tool {
  "data_source_name": "hust_cnki_thesis",
  "api_name": "search_theses" | "get_thesis_detail" | "download_thesis" | "read_thesis"
            | "list_theses" | "check_access" | "open_login",
  "params": { ... }
}
```

返回恒为 JSON 文本：成功 `{"is_success": "...", "data_preview": "...", ...}`；
失败 `{"is_success": false, "error": "...", "error_kind": "...", "guidance": "...", "screenshot": "..."}`。
末尾带 `[hust-library-thesis] request-id: ... · api: ...` 行（报障时可引用）。

**核心约定：结果落盘 + 预览回传。** 完整数据在 `file_path`（CSV/markdown），回传只有表头+前几行。
要更多行就读文件，**不要反复检索**。

## 4. 七个 API 速查

| API | 必填 | 关键可选 | 产物 |
|---|---|---|---|
| `search_theses` | `query`, `file_path` | `field`(SU主题/TI题名/KY/AB/AU), `num_results`(≤50), `school`, `degree`(硕士/博士), `start_year`/`end_year`, `headless` | 结果 CSV |
| `get_thesis_detail` | `handle`, `file_path` | `query`, `title`, `index`, `headless` | 详情 MD（摘要/关键词/DOI/导师/TOC/全文按钮） |
| `download_thesis` | `handle` 或 `query` | `title`, `index`, `save_dir`, `extract_text` | PDF + manifest 登记；`extract_text=true` 另产全文 MD |
| `read_thesis` | `handle` | `pages`("1-8"/"52-56"/"all"), `max_chars`(默认20000), `out_path` | 分页文本（直接进上下文） |
| `list_theses` | `file_path` | `save_dir` | 已下载清单 CSV |
| `check_access` | 无（建议给 `file_path` 存截图） | `headless`(默认 true) | 闸门/机构态体检 |
| `open_login` | 无 | — | 弹有头浏览器一次性登录，后台守护进程轮询，立即返回 |

**handle 三种形态**：① 检索 CSV 里的 `url`（最稳，但 v 令牌与会话绑定，跨会话/久置会 403）；
② manifest 键（DOI 去符号，如 `10.27440_d.cnki.gysdu.2025.003455`，用于已下载论文）；
③ `库名:文件名`（如 `CMFD202102:XIDIAN2021.002354`，兜底）。

## 5. 标准工作流（已实测样例）

### 5.1 找某主题的最新论文 → 挑一篇读全文

```jsonc
// ① 主题检索（用户要"最新"就加年度区间）
call_data_source_tool { "api_name": "search_theses",
  "params": { "query": "水下机器人", "field": "SU",
              "start_year": 2025, "end_year": 2026, "num_results": 20,
              "file_path": "D:/文献/知网学位论文/水下机器人_2025-2026.csv" } }
// → is_success 里带 total_results_on_site；data_preview 只有前 5 行，完整清单读 CSV

// ② 看详情（摘要/目录），判断是否值得下载
call_data_source_tool { "api_name": "get_thesis_detail",
  "params": { "handle": "<CSV里的url>", "query": "水下机器人", "title": "<题名前20字>",
              "file_path": "D:/文献/知网学位论文/xxx_详情.md" } }

// ③ 下载全文（消耗机构配额！先确认用户要这篇）
call_data_source_tool { "api_name": "download_thesis",
  "params": { "handle": "<url>", "query": "水下机器人", "title": "<题名前20字>",
              "extract_text": true } }
// → 返回 pdf_path / text_path / pages / manifest_key

// ④ 按页阅读（摘要在前几页；读正文按目录页码换算）
call_data_source_tool { "api_name": "read_thesis",
  "params": { "handle": "<manifest_key>", "pages": "4-11", "max_chars": 16000 } }
```

实测基准：《感知受限下遥操作水下机器人协同控制策略研究》（2025·博士）147 页 17MB，
检索→详情→下载→读摘要/结论全链路无人工干预跑通。

### 5.2 用户点名某篇论文（按题名精确找）

```jsonc
// 用 field="TI" 题名检索，第一行即目标；query 与 title 都给全题名
call_data_source_tool { "api_name": "search_theses",
  "params": { "query": "感知受限下遥操作水下机器人协同控制策略研究", "field": "TI",
              "file_path": "D:/文献/知网学位论文/精确检索.csv", "num_results": 5 } }
// 之后 detail/download 同 5.1，title 传完整题名
```

### 5.3 用户说"读你下载过的那篇"

先 `list_theses` 拿清单 → `read_thesis(handle=<manifest_key>, pages=...)`。
**不要**为了阅读重新下载——manifest 里已有 PDF/MD 路径，read 直接用。

## 6. 必须遵守的纪律（违反会浪费配额或触发风控）

1. **query/title 尽量总带上**（detail/download 均支持）。知网详情页 `v=` 令牌与会话绑定，
   检索后时间一长直开 handle 会 403；带 `query` 会自动走单会话链路（检索→点行→详情→下载）。
   **给了 `title` 却匹配不到时服务会报错而不是盲点行号**（曾因此下错过论文，已加固）。
2. **下载 = 消耗机构配额**：单次调用只允许一篇；`batch` 参数会被直接拒绝。
   用户没点名哪篇之前，最多到 detail 为止，**不要主动下载**。批量必须用户明确授权。
3. **频率纪律**：一次会话 1-2 组检索词；翻页靠 `num_results` 而不是反复调用。
4. **桌面会短暂弹出 Edge 窗口**（默认有头、真实 Edge 指纹）——这是抗知网瑞数风控的设计，
   不是故障；不要建议用户改无头（`HUST_LT_HEADLESS=1` 会 403）。
5. **验证码是人工红线**：`captcha_required` 返回时附 screenshot 路径，如实告知用户，
   由人完成滑块后再试；绝不重试轰炸。
6. **read 优先于 download**：manifest 里已有的论文不要重复下载（`read_thesis` 会先查清单）。
7. `file_path` 用绝对路径；相对路径落到服务 `out/datasource/` 下。

## 7. 失败处理表

| `error_kind` | 含义 | agent 应该做什么 |
|---|---|---|
| `login_required` | 知网右上角无机构态（校外/掉登录） | 告知用户将弹出浏览器 → 调 `open_login` → 用户完成「统一身份认证登录 + 点中国知网行的智能访问」→ `check_access` 确认 → 重试原操作 |
| `captcha_required` | 知网滑块/安全验证 | 停手，把 screenshot 路径给用户，请其人工完成（可借 `open_login` 的有头窗口）；几分钟后重试，放慢频率 |
| `rate_limited` | 今日下载配额用尽 | 当天停止下载；建议改天或人工章节下载 |
| `channel_error` | 通道/网络/子进程问题 | 按 `error` 文案处理：handle 失效 → 带 `query`+`title` 重试；标题校验失败 → 改 `field="TI"` 精确检索；仍失败 → 检查 playwright/Edge |
| `unexpected` | 未预期异常 | 原样把 error 呈现给用户，勿盲目重试 |

## 8. 排错速查

| 症状 | 原因 | 处置 |
|---|---|---|
| search 返回 0 行但 `total_results_on_site` > 0 | 结果表 JS 渲染竞态（已内置二次抓取） | 换更具体的词重试一次；或用 `field="TI"` |
| detail/download 报「v 令牌与会话绑定…403」 | handle 跨会话失效 | 带 `query`(+`title`) 重新调用 |
| 报「检索结果首页里没有标题含…的行」 | 知网排序波动，目标不在第一页 | 改用 `field="TI"` + 题名精确检索；不要指望按行号盲点 |
| 报「标题校验失败：请求《A》，详情页实际是《B》」 | 保护性拦截（未消耗配额） | 用 `field="TI"` 重试 |
| 子进程 rc≠0、结果文件未生成 | Edge/playwright 环境异常 | 重试一次；检查 `.browser-profile` 是否被锁（删 `Singleton*` 文件） |
| 工具长时间不返回 | 下载大 PDF 正常需 1-3 分钟 | 客户端超时建议 ≥600s（ZCode 已配 `timeoutMs: 600000`） |

## 9. 环境变量（按需覆盖）

| 变量 | 默认 | 用途 |
|---|---|---|
| `HUST_LT_HEADLESS` | `0`（有头） | `1` 强制无头——风控风险自担，不推荐 |
| `HUST_LT_PROFILE` | `D:\hust-library-thesis\.browser-profile` | 换新档案（被风控标记后换道重试） |
| `HUST_LT_CLASSID` | `LSTPFY1C` | kns8s 学位论文库 classid |

## 10. 项目文件地图

```
D:\hust-library-thesis\
├── server\hust_library_thesis_server.py   # MCP 服务器（FastMCP 单文件）
├── .browser-profile\                      # Playwright 持久档案（登录态/风控 cookie）
├── out\datasource\                        # 检索/详情/清单默认落盘处
├── out\downloads\                         # PDF + 全文MD + manifest.json（键=DOI）
├── out\shots\                             # 风控/登录/超时截图（人工核对用）
├── out\login_state.json                   # open_login 守护进程状态（pending/success/timeout）
├── tests\test_client.py                   # stdio 冒烟客户端（不依赖 ZCode 也能自检）
├── README.md                              # 人类视角说明
└── AGENT_GUIDE.md                         # 本文件
```

服务自检命令（任何 agent 都可跑）：

```bash
python D:\hust-library-thesis\tests\test_client.py desc          # 工具与文档
python D:\hust-library-thesis\tests\test_client.py access        # 通道体检
python D:\hust-library-thesis\tests\test_client.py search "舰船规避" 5
```
