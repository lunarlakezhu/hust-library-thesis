---
name: hust-library-thesis
description: 华中科技大学图书馆（数字资源统一访问平台）→ 中国知网学位论文检索/详情/全文下载/本地阅读的 MCP（hust_cnki_thesis 数据源，契约对齐 kimi-datasource）。当用户要找中文硕博学位论文、要下知网学位论文 PDF、要读学位论文全文，或提到华科图书馆/知网/CNKI 学位论文时使用。期刊论文请用其他渠道；判例/法条用 lawkit/examplekit。
---

# hust-library-thesis：华科图书馆 → 知网学位论文

> 完整 agent 参考文档（接入其他客户端、调用配方、排错速查）：
> **D:\hust-library-thesis\AGENT_GUIDE.md** —— 本 skill 是行为摘要，遇未覆盖情况查该文档。

复刻「访问知网阅读文献」视频的完整链路：**数字资源统一访问平台 → 智能访问 → 知网学位论文库 → 检索 → 详情 → 原版阅读/PDF 下载 → 本地阅读**。

## 工具契约（与 kimi-datasource 完全同形，永远先查文档再调用）

1. `get_data_source_desc(name="hust_cnki_thesis")` — 取 API 文档（参数以文档为准）
2. `call_data_source_tool(data_source_name="hust_cnki_thesis", api_name=..., params=...)`

可用 API：`check_access`（体检）/ `open_login`（一次性登录）/ `search_theses`（检索，CSV 落盘）/
`get_thesis_detail`（详情，md 落盘）/ `download_thesis`（PDF 下载）/ `read_thesis`（本地全文阅读）/
`list_theses`（已下载清单）。

## 标准流程

```
search_theses(query="舰船规避", file_path="D:/文献/知网/舰船规避.csv", num_results=10)
  → CSV：title,authors,school,degree,year,cited,downloads,url,…（url 是后续调用的 handle）
get_thesis_detail(handle=<url>, query="舰船规避", title="<题名前20字>", file_path=…)
  → 摘要/关键词/DOI/导师/文章目录/可用全文方式
download_thesis(handle=<url>, query="舰船规避", title=…, extract_text=true)
  → pdf_path + text_path（全文 markdown）
read_thesis(handle=<manifest键或url>, pages="52-56")
  → 分页文本，供直接引用分析
```

**关键纪律：**

- **query/title 尽量总带上**（detail/download 均支持）：知网 v 令牌与会话绑定，检索后时间一长
  直开 handle 会 403；带 query 会自动走单会话链路（检索→点行→详情→下载），最稳。
- **结果落盘 + 预览回传**：完整数据在 `file_path`（CSV/markdown），回传只有
  `is_success`/`data_preview`/`total`。需要更多行时读文件，不要反复检索。
- **频率纪律**：办案节奏，一次 1-2 组检索词；严禁批量抓取、批量下载。
- **下载消耗机构配额**：单次只允许一篇（`batch` 参数直接拒绝）；批量必须用户明确授权。
- file_path 用绝对路径（如 `D:/文献/知网/xxx.csv`）；相对路径会落到服务 `out/datasource/`。

## 失败处理（如实返回，不找旁路）

| error_kind | 含义 | 动作 |
|---|---|---|
| `login_required` | 未识别到机构态（右上角无「华中科技」） | 调 `open_login`（弹有头窗口，用户用统一身份认证登录并点「智能访问」），完成后 `check_access` 确认 |
| `captcha_required` | 知网滑块/安全验证 | `open_login` 开有头窗口人工完成验证后重试；放慢频率。返回值带 screenshot 路径 |
| `rate_limited` | 今日下载上限 | 当天停止下载 |
| `channel_error` | 通道/网络问题 | 按 error 文案处理；handle 失效时带 query 重试 |

## 环境事实（2026-10-27 实测）

- 本机网络被知网识别为华中科技大学机构态（`check_access` 的 `cnki_institutional: true`），
  检索/详情/下载通常开箱可用；`open_login` 是备用通道（校外未登录时必需）。
- 检索默认走 kns8s 学位论文库（classid=LSTPFY1C，主题检索），知网对无头浏览器风控极严
  （瑞数级），本服务默认**有头** Edge 指纹——检索时桌面会短暂弹出浏览器窗口，属正常现象。
- 浏览器层跑在子进程（Playwright 持久档案在项目根 `.browser-profile/`），结果经文件回传；
  服务端点：`D:\hust-library-thesis\server\hust_library_thesis_server.py`（stdio MCP）。
- 已验证样例：《基于TDSBR的海上目标与角反射器复合时域电磁散射及干扰分析研究》检索→详情→
  132 页 PDF 下载（9.5MB）→ read_thesis 按页读取，与视频流程一致。
