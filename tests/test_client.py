# -*- coding: utf-8 -*-
"""MCP stdio 客户端冒烟测试：list_tools -> get_data_source_desc -> search_theses。

用法:
  python tests/test_client.py desc
  python tests/test_client.py search "舰船规避" 5
  python tests/test_client.py detail <url|db:fn>
  python tests/test_client.py access
  python tests/test_client.py call <api_name> '<json params>'
"""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = str(Path(__file__).resolve().parents[1] / "server" / "hust_library_thesis_server.py")
OUT_DIR = Path(SERVER).parents[1] / "out" / "datasource"
PYTHON = sys.executable


async def run(mode: str, *args):
    sp = StdioServerParameters(command=PYTHON, args=[SERVER])
    async with stdio_client(sp) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print(f"TOOLS: {[t.name for t in tools.tools]}")
            if mode == "desc":
                res = await s.call_tool("get_data_source_desc", {"name": "hust_cnki_thesis"})
                print(res.content[0].text[:1200])
            elif mode == "call":
                api = args[0]
                params = json.loads(args[1]) if len(args) > 1 else {}
                res = await s.call_tool("call_data_source_tool",
                                        {"data_source_name": "hust_cnki_thesis", "api_name": api,
                                         "params": params})
                print(res.content[0].text)
            elif mode == "search":
                q = args[0]
                n = int(args[1]) if len(args) > 1 else 5
                out = str(OUT_DIR / f"test_{q}.csv")
                res = await s.call_tool("call_data_source_tool", {
                    "data_source_name": "hust_cnki_thesis", "api_name": "search_theses",
                    "params": {"query": q, "num_results": n, "file_path": out}})
                print(res.content[0].text)
            elif mode == "detail":
                res = await s.call_tool("call_data_source_tool", {
                    "data_source_name": "hust_cnki_thesis", "api_name": "get_thesis_detail",
                    "params": {"handle": args[0]}})
                print(res.content[0].text[:2500])
            elif mode == "access":
                res = await s.call_tool("call_data_source_tool", {
                    "data_source_name": "hust_cnki_thesis", "api_name": "check_access",
                    "params": {"headless": True}})
                print(res.content[0].text)


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else "desc", *sys.argv[2:]))
