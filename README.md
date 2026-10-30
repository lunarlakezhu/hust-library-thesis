# hust-library-thesis

华中科技大学图书馆（数字资源统一访问平台 libresource.hust.edu.cn）→ 中国知网（CNKI）
**学位论文** 的 MCP 服务。对外契约对齐 kimi-datasource：`get_data_source_desc` +
`call_data_source_tool` 两个工具，结果按 `file_path` 落盘、只回传预览。

> **Agent 用户请读 [AGENT_GUIDE.md](AGENT_GUIDE.md)** —— 面向 AI agent 的完整使用说明
> （接入方式、调用配方、纪律、失败处理、排错速查）；本 README 是人类视角概览。

复刻视频《访问知网阅读文献》的完整链路：
数字资源统一访问平台 → 智能访问 → 知网学位论文库 → 检索 → 详情 → PDF 下载 → 本地分页阅读。

## 工具面

| 工具 | 说明 |
|---|---|
| `get_data_source_desc(name)` | 单数据源 API 文档（markdown），先查再用 |
| `call_data_source_tool(data_source_name, api_name, params)` | 分发调用，返回 `is_success` + `data_preview` |

数据源 `hust_cnki_thesis` 的 7 个 API：

- `check_access` — 通道体检（智能访问闸 + 知网机构态识别），轻量、默认无头
- `open_login` — 弹有头浏览器做一次性登录（统一身份认证 → 智能访问），后台守护进程轮询，
  登录态持久化在 `.browser-profile/`
- `search_theses` — kns8s 学位论文库检索，CSV 落盘（title/authors/school/degree/year/cited/downloads/url）
- `get_thesis_detail` — 详情结构化抓取（摘要/关键词/DOI/导师/TOC/全文按钮），md 落盘
- `download_thesis` — PDF 下载（bar.cnki.net 下单链），落盘 + manifest 登记（键=DOI），
  可选 `extract_text` 抽全文 markdown；单次一篇，批量需显式授权（`batch` 参数直接拒绝）
- `read_thesis` — 本地 PDF 分页文本读取（PyMuPDF），`pages="1-8"/"52-56"/"all"`
- `list_theses` — 已下载清单 CSV

## 目录

```
D:\hust-library-thesis\
├── server\hust_library_thesis_server.py   # MCP 服务器（单文件，FastMCP）
├── .browser-profile\                       # Playwright 持久档案（登录态/风控 cookie）
├── out\
│   ├── datasource\                         # 检索/详情/清单默认落盘处
│   ├── downloads\                          # PDF + manifest.json
│   └── shots\                              # 风控/登录截图（人工核对用）
└── tests\test_client.py                    # stdio 冒烟客户端
```

## 注册（ZCode）

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

改完重启 ZCode 生效。配套 skill 在 `C:\Users\<用户名>\.agents\skills\hust-library-thesis\SKILL.md`。

## 设计要点（与 example-kit case_online 同源纪律）

- **浏览器走子进程**：Playwright 在 `--op` 子进程里运行，结果经文件回传——浏览器组件污染
  stdout 会破坏 MCP framing（实测踩坑：管道写端被 Edge 孙进程继承导致 communicate 卡死，
  故 stdin/stdout/stderr 全部与传输层隔离）。
- **有头 Edge 指纹**：知网（kns.cnki.net / bar.cnki.net）为瑞数级风控，无头浏览器直接 403；
  本服务优先 `channel="msedge"`（真实 Edge 指纹）+ 有头运行，检索时桌面短暂弹窗属正常。
- **v 令牌与会话绑定**：检索结果的详情 url 带 `v=` 令牌，跨浏览器会话会 403。
  detail/download 均支持 `query`（+`title`/`index`）参数：直开失败时在同一会话内重新检索并
  点入结果行——即视频的原生流程。
- **风控红线**：安全验证=人工红线，截图停机（`captcha_required` + screenshot 路径），
  绝不程序化破解。
- **失败如实返回**：`login_required` / `captcha_required` / `rate_limited` / `channel_error`，
  附 guidance 与截图路径。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HUST_LT_HEADLESS` | `0`（有头） | `1` 强制无头（风控风险自担） |
| `HUST_LT_PROFILE` | 项目 `.browser-profile` | 换新档案（被风控标记后换道重试） |
| `HUST_LT_CLASSID` | `LSTPFY1C` | kns8s 检索库 classid |

## 依赖

Python 3.12 + `mcp`（FastMCP）+ `playwright`（含 msedge channel）+ `pymupdf`。
