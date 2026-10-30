# -*- coding: utf-8 -*-
"""hust-library-thesis — 华中科技大学图书馆 → 知网（CNKI）学位论文 MCP。

对外契约完全对齐 kimi-datasource（两个工具）:
  get_data_source_desc(name)                     -> 单个数据源的 API 文档（markdown）
  call_data_source_tool(data_source_name, api_name, params) -> 分发调用

数据源 hust_cnki_thesis 的能力（复刻视频「访问知网阅读文献」的完整链路）:
  check_access        通道体检：libresource 智能访问闸 + 知网机构号识别
  open_login          打开有头浏览器做一次性登录（统一身份认证 → 智能访问），后台守护进程轮询
  search_theses       学位论文检索（kns8s, classid=LSTPFY1C），结果 CSV 落盘 + 预览回传
  get_thesis_detail   详情页结构化抓取（摘要/关键词/DOI/导师/全文按钮），markdown 落盘
  download_thesis     PDF 下载（走 bar.cnki.net 下单链），需机构权限，落盘+登记 manifest
  read_thesis         本地已下载 PDF 的全文读取（PyMuPDF，页码区间，供模型阅读）
  list_theses         已下载论文清单 CSV

设计纪律（与 example-kit case_online 同源）:
  - 浏览器档案持久化在项目根 .browser-profile/，登录一次跨进程复用；
  - 验证码/风控 = 人工红线：截图停机（CaptchaRequired），绝不程序化破解；
  - 失败如实返回（login_required / captcha_required / rate_limited），不找旁路；
  - 下载消耗机构配额：单次只下一篇，批量需用户明确授权。

结果落盘约定同 kimi-datasource：list/search 类结果写 file_path（CSV），
回传只带 is_success + data_preview（表头+前几行），全文数据在文件里。
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = Path(os.environ.get("HUST_LT_PROFILE", str(ROOT / ".browser-profile")))
OUT_DIR = ROOT / "out"
SHOTS_DIR = OUT_DIR / "shots"
DOWNLOADS_DIR = OUT_DIR / "downloads"
DATASOURCE_DIR = OUT_DIR / "datasource"
MANIFEST_FILE = DOWNLOADS_DIR / "manifest.json"
LOGIN_STATE_FILE = OUT_DIR / "login_state.json"
SERVER_FILE = Path(__file__).resolve()

# HUST_LT_HEADLESS=1 强制无头；默认有头（与 example-kit 同纪律：有头指纹真实、风控误伤少）。
# check_access 属轻量体检，默认无头。
HEADLESS_DEFAULT = os.environ.get("HUST_LT_HEADLESS", "0") == "1"

LIBRESOURCE_DB_NAV = "https://libresource.hust.edu.cn/next/resource/databases/navigation"
LIBRESOURCE_GO_CNKI = "https://libresource.hust.edu.cn/go?url=https%3A%2F%2Fwww.cnki.net"
CNKI_HOME = "https://www.cnki.net"
CNKI_THESIS_SEARCH = ("https://kns.cnki.net/kns8s/search?classid={classid}&kw={kw}"
                      "&korder={field}&language=CHS&uniplatform=NZKPT")
THESIS_CLASSID = os.environ.get("HUST_LT_CLASSID", "LSTPFY1C")  # 学位论文总库
DETAIL_ASX = "https://kns.cnki.net/KCMS/detail/detail.aspx?dbcode={code}&dbname={db}&filename={fn}"
DETAIL_KCMS2 = "https://kns.cnki.net/kcms2/article/abstract?dbcode={code}&dbname={db}&filename={fn}"

INSTITUTION_MARK = "华中科技"
FULLTEXT_BUTTONS = ("CNKI AI阅读", "原版阅读", "CAJ下载", "PDF下载", "章节下载", "HTML阅读", "我是作者，免费下载")
SEARCH_FIELDS = {"SU": "主题", "TI": "题名", "KY": "关键词", "AB": "摘要", "AU": "作者", "FT": "全文"}

CSV_COLUMNS = ["title", "authors", "school", "degree", "year", "cited", "downloads",
               "filename", "db_name", "url"]

mcp = FastMCP("hust-library-thesis")

# 同一浏览器档案不允许并发打开（persistent context 档案锁）
_CHANNEL_LOCK = threading.Lock()
BROWSER_APIS = {"check_access", "search_theses", "get_thesis_detail", "download_thesis"}
_CHANNEL_LOCK_DSC = """# hust_cnki_thesis

**Description:** 华中科技大学图书馆数字资源统一访问平台（libresource.hust.edu.cn）「智能访问」闸
进入中国知网（CNKI），面向学位论文（博士学位论文全文库 CMFD + 优秀硕士学位论文全文库 CDFD）
的检索、详情、全文下载与本地阅读通道。全文权限来自机构订阅；离线/校外未登录时检索与详情仍可用，
下载与原版阅读需要先走 open_login 一次性登录。浏览器（Playwright 持久档案）由本服务托管。

## Available APIs

### check_access

**Description:** 通道体检。经 libresource 智能访问闸走一遍跳转，再检查知网首页右上角是否出现
机构名（华中科技大学）。返回各环节状态与建议动作。登录守护进程运行中时返回 pending。

**Required Parameters:**
- `file_path` (string): 截图保存路径（png）。绝对路径推荐；相对路径会落到服务的 out/ 目录。

**Optional Parameters:**
- `headless` (boolean): 是否无头执行（默认 true；轻量体检用无头即可）

---

### open_login

**Description:** 打开一次有头浏览器完成机构登录：在弹出的窗口里用华中科技大学统一身份认证登录
libresource → 登录成功后点「中国知网（CNKI）系列数据库」行的「智能访问」→ 新标签知网首页右上角
出现「华中科技」即成功。工具立即返回，后台守护进程轮询（约 10 分钟）并把结果写入状态文件；
之后用 check_access 确认。登录态持久化，通常一次即可。

**Required Parameters:** 无

---

### search_theses

**Description:** 知网学位论文检索。复刻视频链路：kns8s 检索页（classid=LSTPFY1C，学位论文库），
抓取结果表（中文题名/作者/学位授予单位/数据库/学位授予年度/被引/下载）。school/degree/year 为
本地后过滤（先取回再筛，结果数请给足）。结果 CSV 落盘，回传预览。

**Required Parameters:**
- `query` (string): 检索词。不要超过 6 个独立关键词；不要用 'OR' 连接。
- `file_path` (string): 结果保存路径，CSV 格式。绝对路径推荐（如 D:/文献/知网学位论文/xxx.csv）。

**Optional Parameters:**
- `num_results` (integer): 需要的结果条数（默认 10，≤50；翻页抓取，每页 20）
- `field` (string): 检索入口字段：SU 主题（默认）/ TI 题名 / KY 关键词 / AB 摘要 / AU 作者
- `school` (string): 学位授予单位包含匹配（如「华中科技大学」）
- `degree` (string): 学位级别：硕士 / 博士
- `start_year` (integer): 学位授予年度下限
- `end_year` (integer): 学位授予年度上限
- `headless` (boolean): 默认 false（有头窗口会短暂出现，指纹更真实、风控误伤少）

---

### get_thesis_detail

**Description:** 抓取某篇学位论文的详情页：题名/作者/导师/学位授予单位/摘要/关键词/DOI/分类号/
学科专业/下载量/可用全文按钮（PDF下载、原版阅读、章节下载…）。markdown 落盘并回传预览。

**Required Parameters:**
- `handle` (string): search_theses 结果里的 `url`（须在检索后尽快使用，v 令牌与会话绑定）。
- `file_path` (string): 详情 markdown 保存路径。

**Optional Parameters:**
- `query` (string): 与检索词一致。给了它，handle 打不开（v 令牌过期/跨会话）时会自动在
  同一会话里重新检索并点入结果行——**推荐总是带上**。
- `title` (string): 结果行标题（或前 20 字），用于在新检索里定位同一篇。
- `index` (integer): 无 title 时的行号（0 基，默认 0=第一行）。
- `headless` (boolean): 默认 false（有头窗口会短暂出现）

---

### download_thesis

**Description:** 下载某篇学位论文的 PDF（知网 bar.cnki.net 下单链）。**消耗机构下载配额，
一次只允许一篇；批量下载必须先获得用户明确授权。** 需要机构权限（右上角「华中科技」），
未登录时如实返回 login_required 并附截图。成功后登记 manifest，可选抽取全文 markdown。

**Required Parameters:**
- `handle` (string): 同 get_thesis_detail；或与 `query` 二选一。

**Optional Parameters:**
- `query` (string): 检索词。给出后走单会话链路（检索→点行→详情→下载），最稳；推荐总是带上。
- `title` (string): 结果行标题（或前 20 字），用于定位同一篇。
- `index` (integer): 无 title 时的行号（0 基，默认 0）。
- `save_dir` (string): 保存目录（默认服务 out/downloads/）
- `extract_text` (boolean): 下载后用 PyMuPDF 抽取全文 markdown（默认 false）
- `headless` (boolean): 默认 false（有头窗口，可人工应对弹窗）

---

### read_thesis

**Description:** 阅读本地已下载的学位论文全文（PyMuPDF 抽取文本，供模型直接阅读）。
相当于 kimi-datasource arxiv 的 read_paper。首次读取可传 out_path 顺手落盘 markdown。

**Required Parameters:**
- `handle` (string): 同 get_thesis_detail（会先查本地下载清单）。

**Optional Parameters:**
- `pages` (string): 页码区间，如 "1-8"、"52-56"、"all"（默认 "1-8"，封面+摘要最前置）
- `max_chars` (integer): 返回文本上限（默认 20000，超长截断并注明）
- `out_path` (string): 顺带保存全文/区间 markdown 的路径（可选）

---

### list_theses

**Description:** 列出本地已下载的学位论文（来自下载 manifest 与 out/downloads 扫描），CSV 落盘。

**Required Parameters:**
- `file_path` (string): 清单 CSV 保存路径。

**Optional Parameters:**
- `save_dir` (string): 只列该目录（默认 out/downloads/）

---

## 约定与纪律
- 结果落盘 + 预览回传：完整数据在 file_path，回传只带 is_success / data_preview / total。
- file_path 建议绝对路径；相对路径统一落到服务 out/datasource/ 下。
- 频率纪律：办案节奏，一次 1-2 组检索词；严禁批量抓取。下载消耗机构配额，单次一篇。
- 风控红线：出现安全验证/滑块即截图停机（captcha_required），由人工在浏览器里完成后重试。
- handle 优先用 search_theses CSV 里的 url（带 v 令牌最稳）；库名:文件名 是次选兜底。
"""


class ChannelError(RuntimeError):
    """可预期的失败（如实返回给调用方）。"""


class LoginRequired(ChannelError):
    def __init__(self, message: str, screenshot: str = ""):
        super().__init__(message)
        self.screenshot = screenshot


class CaptchaRequired(ChannelError):
    def __init__(self, message: str, screenshot: str = ""):
        super().__init__(message)
        self.screenshot = screenshot


def _shot(page, name: str) -> str:
    try:
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        path = SHOTS_DIR / f"{name}.png"
        page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return ""


def _now() -> float:
    return time.time()


def _norm_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://kns.cnki.net" + href
    return href


def _parse_detail_key(href: str) -> tuple[str, str, str]:
    """从链接里解析 (dbcode, dbname, filename)。"""
    try:
        q = parse_qs(urlparse(href).query)
        code = (q.get("dbcode") or [""])[0]
        db = (q.get("dbname") or [""])[0]
        fn = (q.get("filename") or [""])[0]
        if fn and ("v=" in href or not db):
            # kcms2 链接dbname可能在path里，尽力而为
            pass
        return code, db, fn
    except Exception:
        return "", "", ""


def resolve_path(spec: Any, default_dir: Path, suffix: str = ".csv") -> Path:
    """kimi 约定 file_path 优先绝对路径；相对/缺省落到服务 out 目录。"""
    if not spec:
        default_dir.mkdir(parents=True, exist_ok=True)
        return default_dir / f"hust_lt_{time.strftime('%Y%m%d_%H%M%S')}{suffix}"
    p = Path(str(spec)).expanduser()
    if not p.is_absolute():
        default_dir.mkdir(parents=True, exist_ok=True)
        p = default_dir / p
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix == "":
        p = p.with_suffix(suffix)
    return p


def _load_manifest() -> dict:
    try:
        return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_manifest(m: dict) -> None:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")


def _resolve_handle(handle: str) -> tuple[str, str, str]:
    """handle -> (url_or_empty, dbcode, dbname, filename) 的中间形式：返回 (url, db, fn)"""
    h = str(handle or "").strip()
    if not h:
        raise ChannelError("handle 不能为空：给 search 结果里的 url，或 库名:文件名")
    if h.lower().startswith("http"):
        return h, "", ""
    m = _load_manifest()
    if h in m:
        return m[h].get("url", ""), m[h].get("db_name", ""), h
    if ":" in h:
        db, fn = h.split(":", 1)
        return "", "", fn.strip()
    # 裸 filename：清单或下载目录里找
    for fn, rec in m.items():
        if fn == h:
            return rec.get("url", ""), rec.get("db_name", ""), fn
    raise ChannelError(f"handle 无法解析：{h}（可用 search 结果 url 或 库名:文件名）")


# ---------------------------------------------------------------- browser 层

def _launch(headful: bool | None = None, accept_downloads: bool = True):
    from playwright.sync_api import sync_playwright  # 延迟导入，MCP 启动更快
    headless = HEADLESS_DEFAULT if headful is None else (not headful)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    common = dict(
        headless=headless,
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        accept_downloads=accept_downloads,
        args=["--disable-blink-features=AutomationControlled",
              "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
              "--no-first-run", "--no-default-browser-check"],
    )
    # 优先本机真实 Edge（指纹贴近用户日常浏览器，显著降低风控误伤），失败退回内置 Chromium
    ctx = None
    try:
        ctx = pw.chromium.launch_persistent_context(str(PROFILE_DIR), channel="msedge", **common)
    except Exception:
        ctx = None
    if ctx is None:
        ctx = pw.chromium.launch_persistent_context(str(PROFILE_DIR), **common)
    ctx.set_default_timeout(20000)
    return pw, ctx


# 挑战页判定从严：只认「可见的挑战弹层/挑战标题/挑战URL」，不扫正文（正文常含"验证码"字样，误报多）
_CAPTCHA_HINTS = ("向右滑动完成验证", "请完成安全验证", "拖动下方滑块")


def _captcha_visible(page) -> bool:
    try:
        if any(k in (page.title() or "") for k in ("安全验证", "异常流量")):
            return True
        u = (page.url or "").lower()
        if "/verify" in u or "captcha" in u:
            return True
        for hint in _CAPTCHA_HINTS:
            try:
                loc = page.get_by_text(hint, exact=False)
                if loc.count() and loc.first.is_visible():
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def _check_captcha(page, where: str) -> None:
    # 知网的 JS 挑战常为瞬态（数秒自动过）：先复检两轮再停机
    if not _captcha_visible(page):
        return
    for _ in range(2):
        page.wait_for_timeout(3000)
        if not _captcha_visible(page):
            return
    raise CaptchaRequired(f"{where} 触发安全验证（滑块/验证码），已截图停机", _shot(page, f"captcha_{int(_now())}"))


def _institution_ok(page) -> bool:
    try:
        text = page.inner_text("body", timeout=8000)[:4000]
    except Exception:
        return False
    return INSTITUTION_MARK in text


def _goto(page, url: str, timeout: int = 35000, retries: int = 1):
    """导航 + 落定等待。瑞数挑战偶发 403/中断：退避后重试一次。"""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_timeout(2200)
            return
        except Exception as e:
            last = e
            msg = str(e)
            if any(k in msg for k in ("ERR_HTTP_RESPONSE_CODE_FAILURE", "ERR_ABORTED", "ERR_CONNECTION_RESET")):
                page.wait_for_timeout(3000)
                continue
            raise
    assert last is not None
    raise last


# ---------------------------------------------------------------- API 实现

def api_check_access(params: dict) -> dict:
    headless = bool(params.get("headless", True))
    shot_path = resolve_path(params.get("file_path"), SHOTS_DIR, ".png")
    # 登录守护进程运行中？
    try:
        st = json.loads(LOGIN_STATE_FILE.read_text(encoding="utf-8"))
        if st.get("status") == "pending" and _now() - float(st.get("updated_at", 0)) < 900:
            return {"is_success": "hust_cnki_thesis, check_access",
                    "login_daemon": "pending",
                    "message": "登录守护进程运行中，请在弹出的浏览器里完成登录+智能访问，稍后重新 check_access",
                    "state_file": str(LOGIN_STATE_FILE)}
        if st.get("status") == "success" and _now() - float(st.get("updated_at", 0)) < 120:
            pass  # 刚成功，仍然实测验证一次
    except Exception:
        pass

    pw, ctx = _launch(headful=not headless, accept_downloads=False)
    try:
        page = ctx.new_page()
        result: dict = {}
        # 1) 智能访问闸
        try:
            _goto(page, LIBRESOURCE_GO_CNKI)
        except Exception as e:
            result["gate_error"] = f"libresource 闸门打不开：{e}"
        _check_captcha(page, "libresource 闸门")
        gate_url = page.url
        gate_host = urlparse(gate_url).netloc
        on_cnki = "cnki.net" in gate_host
        result["gate_final_url"] = gate_url
        result["gate_passed_to_cnki"] = on_cnki
        if not on_cnki:
            body = ""
            try:
                body = page.inner_text("body", timeout=8000)[:1500]
            except Exception:
                pass
            need_login = ("登录" in body and "统一身份认证" in body) or "authserver" in gate_url or \
                ("login" in gate_url.lower())
            result["libresource_needs_login"] = bool(need_login)
        # 2) 知网机构识别
        if "cnki.net" not in gate_host:
            _goto(page, CNKI_HOME)
        page.wait_for_timeout(1500)
        result["cnki_institutional"] = _institution_ok(page)
        result["screenshot"] = _shot(page, shot_path.stem)
        if result["cnki_institutional"] and result.get("gate_passed_to_cnki"):
            result["verdict"] = "通道可用，可直接检索/下载"
        elif result["cnki_institutional"]:
            result["verdict"] = "知网侧机构态在，但智能访问闸未通过；建议 open_login 重走闸门"
        elif result.get("libresource_needs_login"):
            result["verdict"] = "需要登录：调用 open_login 一次性完成统一身份认证+智能访问"
        else:
            result["verdict"] = "校内外条件未满足（可能不在校园网/VPN 且未登录）：open_login 后按页面指引操作"
        result["is_success"] = "hust_cnki_thesis, check_access"
        return result
    finally:
        pw.stop()


def _login_daemon() -> int:
    """有头登录守护进程：写状态文件，成功/超时退出。"""
    def write(status: str, message: str, screenshot: str = ""):
        LOGIN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOGIN_STATE_FILE.write_text(json.dumps(
            {"status": status, "message": message, "screenshot": screenshot,
             "updated_at": _now()}, ensure_ascii=False, indent=1), encoding="utf-8")

    write("pending", "浏览器已打开，请完成：统一身份认证登录 → 点中国知网行的「智能访问」")
    pw, ctx = _launch(headful=True, accept_downloads=False)
    try:
        page = ctx.new_page()
        try:
            _goto(page, LIBRESOURCE_GO_CNKI)
        except Exception as e:
            write("error", f"闸门打不开：{e}")
            return 1
        deadline = _now() + 600
        last_shot = ""
        while _now() < deadline:
            ok_page = None
            for p in ctx.pages:
                if "cnki.net" in urlparse(p.url).netloc:
                    try:
                        if INSTITUTION_MARK in p.inner_text("body", timeout=3000)[:4000]:
                            ok_page = p
                            break
                    except Exception:
                        continue
            if ok_page is not None:
                last_shot = _shot(ok_page, f"login_success_{int(_now())}")
                write("success", "机构态已识别（知网右上角出现华中科技），登录态已持久化", last_shot)
                return 0
            time.sleep(3)
        write("timeout", "10 分钟内未检测到机构态；请重试 open_login", last_shot)
        return 2
    finally:
        pw.stop()


def api_open_login(params: dict) -> dict:
    flags = 0
    kwargs: dict = {}
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [sys.executable, str(SERVER_FILE), "--login-daemon"],
            cwd=str(ROOT), creationflags=flags, **kwargs,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return {"is_success": "hust_cnki_thesis, open_login", "error": f"守护进程启动失败：{e}",
                "manual": f"手动执行：python {SERVER_FILE} --login-daemon"}
    time.sleep(1.0)
    st = {}
    try:
        st = json.loads(LOGIN_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"is_success": "hust_cnki_thesis, open_login",
            "login_daemon": st.get("status", "pending"),
            "message": "已在桌面打开浏览器。请完成：① 统一身份认证登录 libresource；"
                       "② 点「中国知网（CNKI）系列数据库」行的「智能访问」；"
                       "③ 知网首页右上角出现「华中科技」即成功。完成后调用 check_access 确认。",
            "state_file": str(LOGIN_STATE_FILE)}


def _search_url(query: str, field: str) -> str:
    field = (field or "SU").upper()
    if field not in SEARCH_FIELDS:
        field = "SU"
    return CNKI_THESIS_SEARCH.format(classid=quote(THESIS_CLASSID), kw=quote(query, safe=""), field=field)


def _scrape_rows(page, errors: list[str] | None = None) -> list[dict]:
    rows: list[dict] = []
    table = page.locator("table.result-table-list")
    if table.count() == 0:
        if errors is not None:
            errors.append("table.result-table-list 未找到")
        return rows
    heads = [h.inner_text().strip() for h in table.locator("thead th").all()]
    idx: dict[str, int] = {}
    for i, h in enumerate(heads):
        if h in ("中文题名", "题名"):
            idx["title"] = i
        elif h == "作者":
            idx["author"] = i
        elif h == "学位授予单位":
            idx["school"] = i
        elif h == "数据库":
            idx["db"] = i
        elif h in ("学位授予年度", "年度", "年份"):
            idx["year"] = i
        elif h == "被引":
            idx["cited"] = i
        elif h == "下载":
            idx["dl"] = i
    for tr in table.locator("tbody tr").all():
        try:
            tds = tr.locator("td").all()
            if len(tds) < 5:
                continue

            def cell(key: str) -> str:
                i = idx.get(key)
                if i is None or i >= len(tds):
                    return ""
                return tds[i].inner_text(timeout=3000).strip()

            title_i = idx.get("title", 2)
            if title_i >= len(tds):
                continue
            title_cell = tds[title_i]
            link = title_cell.locator("a.fz14, a[href*='kcms2'], a[href*='detail']").first
            href = ""
            title = title_cell.inner_text(timeout=3000).strip()
            try:
                if link.count():
                    href = link.get_attribute("href") or ""
                    title = link.inner_text(timeout=3000).strip()
            except Exception:
                pass
            code, db, fn = _parse_detail_key(_norm_url(href))
            rows.append({
                "title": title,
                "authors": cell("author").replace("\n", "; "),
                "school": cell("school").replace("\n", " "),
                "degree": cell("db"),
                "year": cell("year"),
                "cited": cell("cited"),
                "downloads": cell("dl"),
                "filename": fn,
                "db_name": db,
                "url": _norm_url(href),
            })
        except Exception as e:
            if errors is not None:
                errors.append(f"{type(e).__name__}: {str(e)[:120]}")
            continue
    return rows


def _parse_total(page) -> str:
    try:
        em = page.locator(".pagerTitleCell em")
        if em.count():
            return em.first.inner_text(timeout=4000).strip()
    except Exception:
        pass
    return ""


def _goto_next_page(page) -> bool:
    for loc in (page.locator("a#PageNext"), page.locator("a.next"), page.locator("a:has-text('下一页')")):
        try:
            if loc.count() and loc.first.is_visible() and "disabled" not in (loc.first.get_attribute("class") or ""):
                loc.first.click()
                page.wait_for_timeout(2500)
                return True
        except Exception:
            continue
    return False


def api_search_theses(params: dict) -> dict:
    query = str(params.get("query") or "").strip()
    if not query:
        raise ChannelError("query 不能为空")
    file_path = resolve_path(params.get("file_path"), DATASOURCE_DIR, ".csv")
    num = int(params.get("num_results") or 10)
    num = max(1, min(num, 50))
    field = str(params.get("field") or "SU")
    school = str(params.get("school") or "").strip()
    degree = str(params.get("degree") or "").strip()
    start_year = params.get("start_year")
    end_year = params.get("end_year")
    headless = bool(params.get("headless", False))

    pw, ctx = _launch(headful=not headless, accept_downloads=False)
    try:
        page = ctx.new_page()
        # 预热：先落 kns 首页拿瑞数挑战 cookie（kns8s 与详情页同域，降 403/风控概率）
        try:
            _goto(page, "https://kns.cnki.net/", timeout=25000)
            page.wait_for_timeout(1500)
        except Exception:
            pass
        _goto(page, _search_url(query, field))
        _check_captcha(page, "检索页")

        def _wait_table(p):
            # 结果表为 JS 渲染：显式等行出现（否则抓空）
            try:
                p.wait_for_selector("table.result-table-list tbody tr", timeout=20000)
            except Exception:
                pass
            p.wait_for_timeout(800)

        _wait_table(page)
        total = _parse_total(page)
        raw: list[dict] = []
        pages = min(5, (num + 19) // 20)
        for pi in range(max(1, pages)):
            _check_captcha(page, f"检索结果第{pi + 1}页")
            row_errs: list[str] = []
            rows_now = _scrape_rows(page, errors=row_errs)
            if not rows_now and pi == 0 and total:
                page.wait_for_timeout(3000)
                rows_now = _scrape_rows(page, errors=row_errs)
            raw.extend(rows_now)
            if not rows_now and pi == 0:
                last_debug = {"total_on_site": total, "page_url": page.url[:120],
                              "row_errors": row_errs[:5],
                              "heads": [h.inner_text().strip() for h in
                                        page.locator("table.result-table-list thead th").all()][:12]}
            if len(raw) >= num or pi == pages - 1:
                break
            if not _goto_next_page(page):
                break
            _wait_table(page)
        out = []
        for r in raw:
            if school and school not in (r["school"] or ""):
                continue
            if degree and degree not in (r["degree"] or ""):
                continue
            try:
                y = int(re.sub(r"\D", "", r["year"] or "") or 0)
            except Exception:
                y = 0
            if start_year and y < int(start_year):
                continue
            if end_year and y > int(end_year):
                continue
            out.append(r)
            if len(out) >= num:
                break
        with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            w.writeheader()
            for r in out:
                w.writerow(r)
        result = {"is_success": f"hust_cnki_thesis, {len(out)} theses saved to {file_path}",
                  "query": query, "field": field, "total_results_on_site": total,
                  "returned": len(out), "data_preview": _csv_preview(file_path, out)}
        if not out and total:
            result["debug"] = last_debug
        return result
    finally:
        pw.stop()


def _csv_preview(file_path: Path, rows: list[dict], limit: int = 5, cell_cap: int = 90) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in rows[:limit]:
        w.writerow([(str(r.get(c, ""))[:cell_cap]) for c in CSV_COLUMNS])
    txt = buf.getvalue()
    if len(rows) > limit:
        txt += f"...（其余 {len(rows) - limit} 行见 {file_path}）\n"
    return txt


def _open_detail(page, handle: str) -> None:
    url, db, fn = _resolve_handle(handle)
    tried: list[str] = []
    if url:
        tried.append(url)
        # 暖机：先落 kns 首页拿瑞数挑战 cookie，再进详情（降低 403 概率）
        try:
            _goto(page, "https://kns.cnki.net/", timeout=25000)
        except Exception:
            pass
        _goto(page, url)
        if _detail_loaded(page):
            return
    if fn:
        code = "CMFD"
        for c in ("CMFD", "CDFD"):
            u = DETAIL_ASX.format(code=c, db=db or f"{c}202102", fn=fn)
            tried.append(u)
            try:
                _goto(page, u)
            except Exception:
                continue
            if _detail_loaded(page):
                return
            u2 = DETAIL_KCMS2.format(code=c, db=db or f"{c}202102", fn=fn)
            tried.append(u2)
            try:
                _goto(page, u2)
            except Exception:
                continue
            if _detail_loaded(page):
                return
    _check_captcha(page, "详情页")
    raise ChannelError(f"详情页打不开（试过 {len(tried)} 个地址）。建议直接用 search_theses 结果里的 url。最后页面：{page.url}")


def _detail_loaded(page) -> bool:
    try:
        if _captcha_visible(page):
            raise CaptchaRequired("详情页触发安全验证，已截图停机", _shot(page, f"captcha_{int(_now())}"))
        # 页面里有若干装饰性 h1（如「自动登录」），只认足够长的正文标题
        for h1 in page.locator("h1").all():
            try:
                if len(h1.inner_text(timeout=3000).strip()) >= 6:
                    return True
            except Exception:
                continue
        return False
    except CaptchaRequired:
        raise
    except Exception:
        return False


_LABEL_PAT = re.compile(r"^(关键词|专辑|专题|DOI|分类号|导师|学科专业|学位年度|学位授予年度|"
                        r"硕士电子期刊出版信息|博士电子期刊出版信息|网络出版时间|页码|页数|学位授予单位|学位级别|密级)\s*[:：]\s*(.+)$")


def _scrape_detail(page) -> dict:
    d: dict = {}
    try:
        # 页面里有「自动登录」等装饰 h1，标题取最长者
        cands = [h.inner_text(timeout=4000).strip() for h in page.locator("h1").all()]
        cands = [c for c in cands if c]
        d["title"] = max(cands, key=len) if cands else ""
    except Exception:
        d["title"] = ""
    # 作者/单位
    for sel, key in (("div.author", "authors"), ("div.orgn", "school"),
                     (".top-tip span[title]", "school"), (".top-tip span", "org_tip")):
        try:
            loc = page.locator(sel)
            if loc.count():
                vals = [x.strip() for x in loc.all_inner_texts() if x.strip()]
                if vals:
                    # school 只取第一个徽标，避免拼接出串味文本
                    d[key] = (vals[0] if key == "school" else "; ".join(vals))[:200]
        except Exception:
            pass
    body = ""
    try:
        body = page.inner_text("body", timeout=8000)
    except Exception:
        pass
    for line in body.splitlines():
        m = _LABEL_PAT.match(line.strip())
        if m:
            k, v = m.group(1), m.group(2).strip()
            if k not in d and v:
                d[k] = v[:500]
    # DOI（也是稳定 manifest 键的来源）
    m = re.search(r"10\.\d{4,5}/d\.cnki\.[\w.]+", body)
    if m and not d.get("DOI"):
        d["DOI"] = m.group(0)
    # 学位授予单位：正文前部单独成行的「xx大学/学院」
    if not d.get("school") and not d.get("学位授予单位"):
        for line in body.splitlines()[:40]:
            t = line.strip()
            if re.fullmatch(r"[\u4e00-\u9fa5]{4,20}(大学|学院)", t):
                d["school"] = t
                break
    # 文章目录（学位论文详情页自带 TOC；在服务推荐噪音前截断）
    try:
        lines = body.splitlines()
        if "文章目录" in body:
            start = next(i for i, ln in enumerate(lines) if "文章目录" in ln)
            noise = ("服务推荐", "AI文献", "个人查重", "AIGC", "推广", "世界各国的", "单脉冲雷达",
                     "雷达对抗", "知网书店", "智能写作", "职称评审", "学位论文投稿")
            toc = []
            for ln in lines[start + 1:start + 120]:
                t = ln.strip()
                if any(n in t for n in noise):
                    break
                if t == "作者简介" and len(toc) > 5:
                    toc.append(t)
                    break
                if t and not t.startswith(("充值", "会员", "个人登录")):
                    toc.append(t)
                if len(toc) >= 60:
                    break
            if toc:
                d["toc"] = toc[:60]
    except Exception:
        pass
    try:
        s = page.locator("#ChDivSummary")
        if s.count():
            d["abstract"] = s.first.inner_text(timeout=5000).strip()
    except Exception:
        pass
    if not d.get("abstract"):
        m = re.search(r"【?摘要】?[:：]?\s*\n?(.{80,4000}?)(?:\n\s*(?:更多|【|关键词|$))", body, re.S)
        if m:
            cand = re.sub(r"\s+", " ", m.group(1)).strip()
            if len(cand) >= 100 and "第" not in cand[:12]:
                d["abstract"] = cand
    # 全文按钮
    opts = []
    reader_url = ""
    try:
        for el in page.locator("a, button").all():
            try:
                t = el.inner_text(timeout=1500).strip()
            except Exception:
                continue
            if any(t == b or (t and b in t) for b in FULLTEXT_BUTTONS) and t not in opts:
                opts.append(t)
                if "原版阅读" in t:
                    href = el.get_attribute("href") or ""
                    reader_url = _norm_url(href) if href else el.get_attribute("data-url") or ""
    except Exception:
        pass
    d["fulltext_options"] = opts
    if reader_url:
        d["reader_url"] = reader_url
    try:
        d["page_url"] = page.url
    except Exception:
        pass
    return d


def _search_in_session(page, query: str, field: str = "SU") -> list[dict]:
    """单会话检索：kns 首页暖机 → 检索页 → 等表 → 抓行。"""
    try:
        _goto(page, "https://kns.cnki.net/", timeout=25000)
        page.wait_for_timeout(1200)
    except Exception:
        pass
    _goto(page, _search_url(query, field))
    _check_captcha(page, "检索页")
    try:
        page.wait_for_selector("table.result-table-list tbody tr", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(800)
    _check_captcha(page, "检索结果页")
    rows = _scrape_rows(page)
    if not rows:
        page.wait_for_timeout(3000)
        rows = _scrape_rows(page)
    return rows


def _click_result_row(page, title: str = "", index: int = 0):
    """点击结果表某行标题链接（0 基；title 给定时按标题匹配）。返回详情页（新标签或当前页）。"""
    table = page.locator("table.result-table-list")
    trs = table.locator("tbody tr")
    if trs.count() == 0:
        raise ChannelError("结果表没有可点击的行")
    target = None
    if title:
        for i in range(min(trs.count(), 25)):
            a = trs.nth(i).locator("a.fz14, a[href*='kcms2']").first
            try:
                if a.count() and title[:20] in (a.inner_text(timeout=3000) or ""):
                    target = a
                    break
            except Exception:
                continue
    if target is None:
        if title:
            raise ChannelError(
                f"检索结果首页（{trs.count()} 行）里没有标题含「{title[:20]}」的行：知网排序波动时"
                f"目标可能不在第一页。请用更精确的检索词（或 field=TI 题名检索）重试；"
                f"绝不按行号盲点，防止下载错论文。")
        if trs.count() <= index:
            raise ChannelError(f"检索结果不足 {index + 1} 行，无法定位目标行（共 {trs.count()} 行）")
        target = trs.nth(index).locator("a.fz14, a[href*='kcms2']").first
    detail = page
    try:
        with page.context.expect_page(timeout=20000) as pop:
            target.click(timeout=10000)
        detail = pop.value
        detail.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass  # 未新开标签：当前页已导航
    detail.wait_for_timeout(2500)
    return detail


def _open_detail_smart(ctx, handle: str, query: str = "", title: str = "", index: int = 0):
    """详情页打开策略：handle 直开（v 令牌新鲜时可用）→ 失败且带 query 时单会话检索点击。
    返回 (detail_page, how)。"""
    page = ctx.new_page()
    if handle:
        try:
            _open_detail(page, handle)
            if _detail_loaded(page):
                return page, "handle"
        except (CaptchaRequired, LoginRequired):
            raise
        except Exception:
            pass
    if not query:
        raise ChannelError(
            "直开详情失败：知网 v 令牌与会话绑定，跨会话会 403。"
            "请在 params 里附 query（与 search_theses 相同的检索词，可选 title 定位），"
            "服务会在同一会话里重新检索并进入详情。")
    rows = _search_in_session(page, query)
    if not rows:
        raise ChannelError(f"单会话检索无结果：query={query}")
    pick = title or ""
    idx = index or 0
    if pick:
        for i, r in enumerate(rows):
            if pick[:20] in (r.get("title") or ""):
                idx = i
                break
    detail = _click_result_row(page, title=pick, index=idx)
    if not _detail_loaded(detail):
        raise ChannelError(f"详情页未加载成功：{detail.url[:120]}")
    return detail, "session_search"


def api_get_thesis_detail(params: dict) -> dict:
    handle = str(params.get("handle") or "")
    query = str(params.get("query") or "").strip()
    title = str(params.get("title") or "").strip()
    index = int(params.get("index") or 0)
    file_path = resolve_path(params.get("file_path"), DATASOURCE_DIR, ".md")
    headless = bool(params.get("headless", False))
    pw, ctx = _launch(headful=not headless, accept_downloads=False)
    try:
        page, how = _open_detail_smart(ctx, handle, query, title, index)
        d = _scrape_detail(page)
        if not d.get("title"):
            raise ChannelError(f"详情页结构未识别（页面：{page.url}），可截图人工核对")
        md = _detail_markdown(d)
        file_path.write_text(md, encoding="utf-8")
        return {"is_success": f"hust_cnki_thesis, detail saved to {file_path}",
                "via": how, "title": d.get("title", ""), "fulltext_options": d.get("fulltext_options", []),
                "data_preview": md[:1600] + ("\n...(完整内容见文件)" if len(md) > 1600 else "")}
    finally:
        pw.stop()


def _detail_markdown(d: dict) -> str:
    lines = [f"# {d.get('title', '')}", ""]
    if d.get("authors"):
        lines.append(f"**作者**: {d['authors']}")
    if d.get("school") or d.get("学位授予单位"):
        lines.append(f"**学位授予单位**: {d.get('school') or d.get('学位授予单位')}")
    lines.append("")
    for k in ("abstract", "关键词", "专辑", "专题", "DOI", "分类号", "导师", "学科专业",
              "学位年度", "学位授予年度", "硕士电子期刊出版信息", "博士电子期刊出版信息",
              "网络出版时间", "页数", "密级"):
        if d.get(k):
            label = "摘要" if k == "abstract" else k
            lines.append(f"- **{label}**: {d[k]}")
    if d.get("fulltext_options"):
        lines.append("")
        lines.append(f"**可用全文方式**: {'、'.join(d['fulltext_options'])}")
    if d.get("toc"):
        lines.append("")
        lines.append("**文章目录**:")
        lines.extend(f"  {t}" for t in d["toc"])
    if d.get("reader_url"):
        lines.append(f"**原版阅读链接**: {d['reader_url']}")
    if d.get("page_url"):
        lines.append(f"**来源页**: {d['page_url']}")
    lines.append("")
    return "\n".join(lines)


def api_download_thesis(params: dict) -> dict:
    handle = str(params.get("handle") or "")
    query = str(params.get("query") or "").strip()
    title = str(params.get("title") or "").strip()
    index = int(params.get("index") or 0)
    if params.get("batch"):
        raise ChannelError("批量下载未授权：本服务单次只允许一篇（下载消耗机构配额）")
    if not handle and not query:
        raise ChannelError("需要 handle（详情 url）或 query（单会话检索下载）至少其一")
    sd = params.get("save_dir")
    if sd:
        save_dir = Path(str(sd)).expanduser()
        if not save_dir.is_absolute():
            save_dir = DOWNLOADS_DIR / save_dir
    else:
        save_dir = DOWNLOADS_DIR
    save_dir.mkdir(parents=True, exist_ok=True)
    extract_text = bool(params.get("extract_text", False))
    headful = not bool(params.get("headless", False))

    pw, ctx = _launch(headful=headful, accept_downloads=True)
    try:
        detail, how = _open_detail_smart(ctx, handle, query, title, index)
        if not _institution_ok(detail):
            shot = _shot(detail, f"login_required_{int(_now())}")
            raise LoginRequired("知网未见机构态（右上角无「华中科技」）。请先 open_login 一次性登录，再下载。", shot)
        # 下载前先抓详情（点完下载后弹窗会改变页面）
        d = _scrape_detail(detail)
        # 标题校验：请求带 title 时必须命中，防止点错行消耗配额
        if title:
            dt = (d.get("title") or "").replace(" ", "")
            if title.replace(" ", "")[:12] not in dt:
                raise ChannelError(
                    f"标题校验失败：请求《{title}》，详情页实际是《{d.get('title')}》。未执行下载。")
        # 找 PDF下载 按钮
        downloads: list = []
        ctx.on("download", lambda dl: downloads.append(dl))
        # 页面里有多个同名「PDF下载」锚点（含隐藏副本，点错不触发下载）：
        # 收集全部可见候选，逐个点击尝试
        cands = []
        for loc in (detail.locator("li.btn-dlpdf a"),
                    detail.locator("a:has-text('PDF下载')"),
                    detail.get_by_role("link", name="PDF下载"),
                    detail.locator("[onclick*='pdfDown'], a.btn-dlpdf")):
            try:
                for i in range(loc.count()):
                    el = loc.nth(i)
                    try:
                        if el.is_visible():
                            cands.append(el)
                    except Exception:
                        continue
            except Exception:
                continue
        if not cands:
            avail = [o for o in FULLTEXT_BUTTONS if any(o in t for t in d.get("fulltext_options", []))]
            raise ChannelError(
                f"未找到可见的「PDF下载」按钮；该文献可用方式：{avail or d.get('fulltext_options') or '未知'}。"
                f"若仅有章节下载/CAJ，请人工处理。页面：{detail.url}")
        dl = None
        last_err = ""
        for ci, cand in enumerate(cands[:4]):
            try:
                cand.click(timeout=10000)
            except Exception as e:
                last_err = f"click#{ci}: {type(e).__name__}"
                continue
            deadline = _now() + 35
            while _now() < deadline:
                if downloads:
                    dl = downloads[-1]
                    break
                # 外来弹窗：风控挑战 / 权限拦截（检索/详情自家页脚的固定词不作依据）
                for p in ctx.pages:
                    u = p.url or ""
                    if "kns8s/search" in u or "kcms2/article" in u:
                        continue
                    if _captcha_visible(p):
                        raise CaptchaRequired("下载环节触发安全验证（弹窗滑块），已截图停机",
                                              _shot(p, f"captcha_{int(_now())}"))
                    if u.startswith("about:"):
                        continue
                    try:
                        t = p.inner_text("body", timeout=2000)[:2000]
                    except Exception:
                        continue
                    if any(k in t for k in ("请登录", "余额不足", "已达上限", "没有下载权限",
                                            "请购买", "登录后下载", "访问验证")):
                        shot = _shot(p, f"dl_blocked_{int(_now())}")
                        if "上限" in t:
                            raise RateLimitError(t)
                        raise LoginRequired(t, shot)
                time.sleep(1.5)
            if dl:
                break
        if dl is None:
            shot = _shot(detail, f"dl_timeout_{int(_now())}")
            raise ChannelError(
                f"各可见「PDF下载」候选点击后均未捕获下载流（{last_err or '点击成功但无下载事件'}）。"
                f"知网偶尔对 bar.cnki.net 弹窗风控，可稍后重试；截图：{shot}")
        suggested = re.sub(r'[\\/:*?"<>|]', "_", dl.suggested_filename or f"hust_thesis_{int(_now())}.pdf")
        target = save_dir / suggested
        try:
            dl.save_as(str(target))
        except Exception as e:
            raise ChannelError(f"保存下载文件失败：{e}")
        size = target.stat().st_size if target.exists() else 0
        if size < 10240:
            raise ChannelError(f"下载文件过小（{size}B），大概率是错误页/提示页：{target}")
        rec = {"pdf": str(target), "size": size, "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "url": d.get("page_url", detail.url), "via": how}
        for k in ("title", "authors", "school", "DOI"):
            if d.get(k):
                rec[k] = d[k]
        if d.get("学位授予年度") or d.get("学位年度"):
            rec["year"] = d.get("学位授予年度") or d.get("学位年度")
        _, db, fn = _resolve_handle(handle) if handle else ("", "", "")
        # 稳定 manifest 键：DOI > 检索侧 filename > 文件名 stem
        doi = (rec.get("DOI") or "").strip()
        if doi:
            key = re.sub(r"[^\w.]", "_", doi)
        elif fn:
            key = fn
        else:
            key = target.stem
        rec["db_name"] = db
        m = _load_manifest()
        m[key] = rec
        _save_manifest(m)
        out = {"is_success": f"hust_cnki_thesis, thesis downloaded to {target}",
               "pdf_path": str(target), "size_bytes": size, "manifest_key": key}
        if extract_text:
            md_path = target.with_suffix(".md")
            n_pages = _pdf_to_markdown(target, md_path)
            out["text_path"] = str(md_path)
            out["pages"] = n_pages
            m[key]["md"] = str(md_path)
            _save_manifest(m)
        return out
    finally:
        pw.stop()


class RateLimitError(ChannelError):
    pass


def _pdf_to_markdown(pdf_path: Path, md_path: Path) -> int:
    import fitz  # PyMuPDF
    doc = fitz.open(str(pdf_path))
    parts = [f"# {pdf_path.stem}", ""]
    for i, pg in enumerate(doc, start=1):
        parts.append(f"<!-- 第 {i} 页 -->")
        parts.append(pg.get_text("text"))
        parts.append("")
    n = doc.page_count
    doc.close()
    md_path.write_text("\n".join(parts), encoding="utf-8")
    return n


def api_read_thesis(params: dict) -> dict:
    handle = str(params.get("handle") or "")
    pages = str(params.get("pages") or "1-8").strip()
    max_chars = int(params.get("max_chars") or 20000)
    out_path = params.get("out_path")
    pdf_path = _find_local_pdf(handle)
    if pdf_path is None:
        raise ChannelError(f"本地未找到该论文 PDF（handle={handle}）。先 download_thesis，或用 list_theses 查看已下载。")
    md_cache = pdf_path.with_suffix(".md")
    if md_cache.exists() and not out_path:
        full = md_cache.read_text(encoding="utf-8")
        n_pages = full.count("<!-- 第 ")
    else:
        import fitz
        doc = fitz.open(str(pdf_path))
        n_pages = doc.page_count
        doc.close()
        if out_path:
            _pdf_to_markdown(pdf_path, Path(str(out_path)).expanduser())
            full = Path(str(out_path)).expanduser().read_text(encoding="utf-8")
        else:
            full = None
    if pages.lower() == "all":
        a, b = 1, n_pages
    else:
        m = re.match(r"^(\d+)\s*[-~]\s*(\d+)$", pages) or re.match(r"^(\d+)$", pages)
        if not m:
            raise ChannelError(f"pages 参数格式不支持：{pages}（示例 1-8 / 52 / all）")
        a = int(m.group(1))
        b = int(m.group(2)) if m.lastindex and m.lastindex >= 2 else a
        a, b = max(1, a), min(n_pages, b)
        if a > b:
            a, b = b, a
    if full is not None:
        # 从缓存全文里按页标记切片
        seg = _slice_markdown_pages(full, a, b)
    else:
        import fitz
        doc = fitz.open(str(pdf_path))
        seg = "\n".join([f"<!-- 第 {i} 页 -->\n" + doc[i - 1].get_text("text") for i in range(a, b + 1)])
        doc.close()
    trunc = ""
    if len(seg) > max_chars:
        seg, trunc = seg[:max_chars], f"\n\n…（已截断：区间文本 {len(seg)} 字符，上限 {max_chars}；可用更小页区间或调大 max_chars）"
    return {"is_success": f"hust_cnki_thesis, read {pdf_path.name} pages {a}-{b}",
            "pdf_path": str(pdf_path), "total_pages": n_pages, "pages_read": f"{a}-{b}",
            "text": seg + trunc}


def _slice_markdown_pages(full: str, a: int, b: int) -> str:
    marks = [(m.start(), int(m.group(1))) for m in re.finditer(r"<!-- 第 (\d+) 页 -->", full)]
    if not marks:
        return full
    start = None
    end = len(full)
    for pos, num in marks:
        if num == a:
            start = pos
        if num == b + 1:
            end = pos
            break
    if start is None:
        return full[:0] or "（区间页码不在缓存内）"
    return full[start:end]


def _find_local_pdf(handle: str) -> Path | None:
    h = str(handle or "").strip()
    m = _load_manifest()
    if h in m and m[h].get("pdf"):
        p = Path(m[h]["pdf"])
        if p.exists():
            return p
    # 用 url/file 提示匹配
    cand = None
    if h.lower().startswith("http"):
        _, _, fn = _parse_detail_key(h)
        cand = fn
    elif ":" in h:
        cand = h.split(":", 1)[1].strip()
    else:
        cand = h
    if cand:
        if cand in m and m[cand].get("pdf"):
            p = Path(m[cand]["pdf"])
            if p.exists():
                return p
        for p in DOWNLOADS_DIR.glob("*.pdf"):
            if cand[:12] in p.stem:
                return p
    # 唯一 PDF 时兜底
    pdfs = list(DOWNLOADS_DIR.glob("*.pdf"))
    if len(pdfs) == 1:
        return pdfs[0]
    return None


def api_list_theses(params: dict) -> dict:
    file_path = resolve_path(params.get("file_path"), DATASOURCE_DIR, ".csv")
    save_dir = Path(str(params.get("save_dir") or DOWNLOADS_DIR)).expanduser()
    m = _load_manifest()
    cols = ["filename", "title", "authors", "school", "year", "db_name", "pdf_path", "md_path", "size_bytes", "downloaded_at"]
    rows = []
    for fn, rec in m.items():
        if save_dir and rec.get("pdf") and not str(rec["pdf"]).startswith(str(save_dir)):
            continue
        rows.append({"filename": fn, "title": rec.get("title", ""), "authors": rec.get("authors", ""),
                     "school": rec.get("school", ""), "year": rec.get("year", ""),
                     "db_name": rec.get("db_name", ""), "pdf_path": rec.get("pdf", ""),
                     "md_path": rec.get("md", ""), "size_bytes": rec.get("size", ""),
                     "downloaded_at": rec.get("downloaded_at", "")})
    with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return {"is_success": f"hust_cnki_thesis, {len(rows)} theses listed to {file_path}",
            "data_preview": _csv_preview_cols(file_path, rows, cols)}


def _csv_preview_cols(file_path: Path, rows: list[dict], cols: list[str], limit: int = 5, cell_cap: int = 70) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows[:limit]:
        w.writerow([str(r.get(c, ""))[:cell_cap] for c in cols])
    txt = buf.getvalue()
    if len(rows) > limit:
        txt += f"...（其余 {len(rows) - limit} 行见 {file_path}）\n"
    return txt


APIS = {
    "check_access": api_check_access,
    "open_login": api_open_login,
    "search_theses": api_search_theses,
    "get_thesis_detail": api_get_thesis_detail,
    "download_thesis": api_download_thesis,
    "read_thesis": api_read_thesis,
    "list_theses": api_list_theses,
}


# ---------------------------------------------------------------- MCP 工具面

@mcp.tool()
def get_data_source_desc(name: str) -> str:
    """Get the API documentation for one data source before calling a specific API.

    For a simple lookup, choose exactly one specialized source; do not inspect
    fallback or comparison sources unless the user explicitly asks for comparison."""
    if name == "hust_cnki_thesis":
        return _CHANNEL_LOCK_DSC
    return f"未知数据源：{name}。可用：hust_cnki_thesis（华科图书馆→知网学位论文）"


@mcp.tool()
async def call_data_source_tool(data_source_name: str, api_name: str, params: dict) -> str:
    """Dispatch one call to the data source selected for the user's request.

    Always call get_data_source_desc(name) first, then use an api_name and params
    from that description. When the user names a data source, use that source."""
    rid = uuid.uuid4().hex[:16]
    if data_source_name != "hust_cnki_thesis":
        return json.dumps({"is_success": False, "error": f"未知数据源 {data_source_name}，可用 hust_cnki_thesis"},
                          ensure_ascii=False, indent=1)
    fn = APIS.get(api_name)
    if fn is None:
        return json.dumps({"is_success": False,
                           "error": f"未知 api_name：{api_name}。可用：{', '.join(APIS)}"},
                          ensure_ascii=False, indent=1)
    try:
        if api_name in BROWSER_APIS:
            # 浏览器操作全部经子进程通道（防 stdout 污染 MCP framing）；档案锁串行
            def _locked_call() -> dict:
                if not _CHANNEL_LOCK.acquire(timeout=5):
                    raise ChannelError("浏览器通道忙：另一操作进行中，稍候重试")
                try:
                    return _run_channel_op(api_name, dict(params or {}))
                finally:
                    _CHANNEL_LOCK.release()
            result = await asyncio.to_thread(_locked_call)
        else:
            result = await asyncio.to_thread(fn, dict(params or {}))
    except (LoginRequired, CaptchaRequired, RateLimitError, ChannelError) as e:
        kind = {"LoginRequired": "login_required", "CaptchaRequired": "captcha_required",
                "RateLimitError": "rate_limited"}.get(type(e).__name__, "channel_error")
        payload = {"is_success": False, "error": str(e), "error_kind": kind,
                   "guidance": _guidance(kind)}
        if getattr(e, "screenshot", ""):
            payload["screenshot"] = e.screenshot
        result = payload
    except Exception as e:  # 未预期失败也如实回传
        result = {"is_success": False, "error": f"{type(e).__name__}: {e}", "error_kind": "unexpected"}
    text = json.dumps(result, ensure_ascii=False, indent=1)
    text += f"\n\n[hust-library-thesis] request-id: {rid} · api: {api_name}"
    return text


def _guidance(kind: str) -> str:
    if kind == "login_required":
        return ("调用 hust_cnki_thesis.open_login 打开浏览器，用华科统一身份认证登录并点「智能访问」，"
                "完成后 check_access 确认，再重试本操作。")
    if kind == "captcha_required":
        return ("知网风控触发。调用 hust_cnki_thesis.open_login 开有头窗口，在其中打开 kns.cnki.net "
                "人工完成滑块验证（通过后风控 cookie 持久化），再重试；检索频率放慢（一次 1-2 组词）。")
    if kind == "rate_limited":
        return "今日下载配额已用尽或被限流：今天不要再下载，明天再试或改用章节下载（人工）。"
    return "检查参数（handle 建议用 search_theses CSV 里的 url），或先用 check_access 体检通道。"


def _run_channel_op(op: str, params: dict) -> dict:
    """在独立子进程里跑浏览器操作（Playwright 污染 stdout 会破坏 MCP framing，
    因此浏览器层全部走子进程，结果经文件回传——example-kit case_online 同款架构）。"""
    out_file = OUT_DIR / f"op_{uuid.uuid4().hex}.json"
    cmd = [sys.executable, str(SERVER_FILE), "--op", op,
           "--params", json.dumps(params, ensure_ascii=False), "--out", str(out_file)]
    # 管道会被 Edge 孙进程继承导致 communicate 等 EOF 卡死：
    # stdin 隔离 + stdout/stderr 全部走文件，结果只经 out 文件回传
    with open(os.devnull, "wb") as devnull:
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                                  stdout=devnull, stderr=devnull,
                                  timeout=900, cwd=str(ROOT))
        except subprocess.TimeoutExpired:
            return {"is_success": False, "error": f"通道子进程超时（15 分钟）：{op}",
                    "error_kind": "channel_error"}
    if out_file.exists():
        try:
            return json.loads(out_file.read_text(encoding="utf-8"))
        except Exception as e:
            return {"is_success": False, "error": f"通道结果文件解析失败：{e}",
                    "error_kind": "channel_error"}
    return {"is_success": False,
            "error": f"通道子进程异常退出（rc={proc.returncode}），结果文件未生成；"
                     f"可重试一次，仍失败请检查 playwright/Edge 环境",
            "error_kind": "channel_error"}


def _op_entry(op: str, params: dict, out_file: Path) -> None:
    try:
        result = APIS[op](params)
    except (LoginRequired, CaptchaRequired, RateLimitError, ChannelError) as e:
        kind = {"LoginRequired": "login_required", "CaptchaRequired": "captcha_required",
                "RateLimitError": "rate_limited"}.get(type(e).__name__, "channel_error")
        payload = {"is_success": False, "error": str(e), "error_kind": kind,
                   "guidance": _guidance(kind)}
        if getattr(e, "screenshot", ""):
            payload["screenshot"] = e.screenshot
        result = payload
    except Exception as e:
        result = {"is_success": False, "error": f"{type(e).__name__}: {e}", "error_kind": "unexpected"}
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    for d in (OUT_DIR, SHOTS_DIR, DOWNLOADS_DIR, DATASOURCE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if "--login-daemon" in sys.argv:
        sys.exit(_login_daemon())
    if "--op" in sys.argv:
        i = sys.argv.index("--op")
        op = sys.argv[i + 1]
        j = sys.argv.index("--params")
        params = json.loads(sys.argv[j + 1])
        k = sys.argv.index("--out")
        _op_entry(op, params, Path(sys.argv[k + 1]))
        sys.exit(0)
    mcp.run()


if __name__ == "__main__":
    main()
