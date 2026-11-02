"""web_search / fetch_url default backends, offline: fake responses and fake DNS."""
from __future__ import annotations

import json
import socket
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from forge import web_backends as wb
from forge.policy import Policy
from forge.tools import ToolContext, build_builtin_registry


class Resp:
    def __init__(self, body=b"", status=200, headers=None):
        self.body, self.status = body, status
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
    def read(self, n=-1): return self.body if n < 0 else self.body[:n]
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def rss(*items):
    rows = "".join(f"<item><title>{t}</title><link>{u}</link><description>{d}</description></item>"
                   for t, u, d in items)
    return Resp(f"<rss><channel>{rows}</channel></rss>".encode(), headers={"Content-Type": "application/rss+xml"})


class Router:
    """Answers requests by host; records every URL so tests can see which engine ran."""
    def __init__(self, **hosts):
        self.hosts, self.seen = hosts, []
    def __call__(self, request, timeout):
        url = request.full_url if isinstance(request, urllib.request.Request) else request
        self.seen.append(url)
        host = urllib.request.urlparse(url).hostname
        answer = self.hosts.get(host)
        if answer is None: raise OSError(f"offline: {host}")
        return answer(url) if callable(answer) else answer


class SearchTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict("os.environ", {"BOCHA_API_KEY": "", "BRAVE_API_KEY": ""})
        patcher.start(); self.addCleanup(patcher.stop)

    def test_auto_region_follows_the_query_language(self):
        router = Router(**{"cn.bing.com": rss(("天气", "https://weather.com.cn/a", "晴")),
                           "www.bing.com": rss(("Weather", "https://weather.com/a", "sunny"))})
        cn = wb.search(query="北京天气", open_=router)
        self.assertEqual((cn[0]["engine"], cn[0]["url"]), ("bing-cn", "https://weather.com.cn/a"))
        self.assertIn("mkt=zh-CN", router.seen[-1])
        us = wb.search(query="weather boston", open_=router)
        self.assertEqual(us[0]["engine"], "bing-us")
        self.assertIn("mkt=en-US", router.seen[-1])
        self.assertEqual(wb.resolve_region("hello", "cn"), ["cn"])
        with self.assertRaises(ValueError): wb.resolve_region("x", "mars")

    def test_both_interleaves_regions_and_drops_duplicates(self):
        router = Router(**{"cn.bing.com": rss(("A", "https://a.cn/", ""), ("Same", "https://same.com/", "")),
                           "www.bing.com": rss(("B", "https://b.com", ""), ("Same", "https://same.com", ""))})
        rows = wb.search(query="x", num=2, region="both", open_=router)
        self.assertEqual([r["url"] for r in rows], ["https://a.cn/", "https://b.com", "https://same.com/"])

    def test_fallback_order_keys_first_then_free_engines(self):
        ddg = Resp(b'<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Fx">'
                   b'<b>X</b> repo</a><a class="result__snippet" href="#">An &amp; snippet</a>')
        router = Router(**{"html.duckduckgo.com": ddg})  # Bing US offline -> DuckDuckGo
        rows = wb.search(query="x repo", open_=router)
        self.assertEqual(rows[0], {"title": "X repo", "url": "https://github.com/x", "snippet": "An & snippet",
                                   "engine": "duckduckgo"})
        brave = Resp(json.dumps({"web": {"results": [{"title": "T", "url": "https://t.com", "description": "<b>d</b>"}]}}).encode())
        with patch.dict("os.environ", {"BRAVE_API_KEY": "k"}):
            router = Router(**{"api.search.brave.com": brave})
            self.assertEqual(wb.search(query="t", open_=router)[0]["engine"], "brave")
            self.assertEqual(router.seen[-1].split("?")[0], "https://api.search.brave.com/res/v1/web/search")
        with self.assertRaises(RuntimeError) as failure:
            wb.search(query="t", open_=Router())
        self.assertIn("bing-us", str(failure.exception))

    def test_site_filter_adds_operator_and_drops_off_domain_results(self):
        router = Router(**{"www.bing.com": rss(("Mod", "https://minecraft.net/x", ""), ("Repo", "https://github.com/a/b", ""))})
        rows = wb.search(query="forge", site="github", open_=router)
        self.assertEqual([r["url"] for r in rows], ["https://github.com/a/b"])
        self.assertIn("site%3Agithub.com", router.seen[-1])
        with self.assertRaises(ValueError): wb.search(query="x", site="not a domain", open_=router)


def dns(mapping):
    def fake(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port))]
    return patch("forge.web_backends.socket.getaddrinfo", side_effect=fake)


class FetchTests(unittest.TestCase):
    PAGE = ("<html><head><title>T &amp; co</title><style>x{}</style></head><body>"
            "<nav>Home | About</nav><div class='site-menu'>Menu</div><div role='navigation'>Links</div>"
            "<main><h1>Heading</h1><p>" + "正文内容。" * 60 + "</p><ul><li>one</li></ul>"
            "<aside>SIDEBAR-AD</aside><script>evil()</script></main><footer>(c)</footer></body></html>")

    def test_article_text_without_page_chrome(self):
        with dns({"news.example": "93.184.216.34"}):
            title, body = wb.fetch(url="https://news.example/a", open_=Router(**{"news.example": Resp(self.PAGE.encode())}))
        self.assertEqual(title, "T & co")
        self.assertTrue(body.startswith("# Heading\n\n正文内容。"), body[:40])
        self.assertIn("- one", body)
        for chrome in ("Home", "Menu", "Links", "SIDEBAR-AD", "evil", "(c)", "x{}"):
            self.assertNotIn(chrome, body.split("[source")[0])

    def test_private_targets_and_redirects_into_them_are_refused(self):
        with dns({"localhost": "127.0.0.1", "router.lan": "192.168.1.1", "meta.cloud": "169.254.169.254"}):
            for url in ("http://localhost/", "http://router.lan/admin", "http://meta.cloud/latest"):
                with self.assertRaisesRegex(ValueError, "private or local"): wb.check_public(url)
        with self.assertRaisesRegex(ValueError, "credentials"): wb.check_public("https://u:p@site.example/")
        with self.assertRaisesRegex(ValueError, "http"): wb.check_public("file:///etc/passwd")
        hop = Resp(status=302, headers={"Location": "http://router.lan/admin"})
        router = Router(**{"open.example": hop, "router.lan": Resp(b"secret")})
        with dns({"open.example": "93.184.216.34", "router.lan": "10.0.0.1"}):
            with self.assertRaisesRegex(ValueError, "private or local"):
                wb.fetch(url="https://open.example/r", open_=router)
        self.assertNotIn("http://router.lan/admin", router.seen)

    def test_fake_ip_proxy_range_counts_as_public(self):
        with dns({"site.example": "198.18.1.47"}):
            self.assertEqual(wb.check_public("https://site.example/"), "https://site.example/")

    def test_encodings_types_and_truncation(self):
        gbk = "<html><head><meta charset='gb2312'><title>标题</title></head><body><p>中文正文</p></body></html>"
        plain = Resp("x" * 9000, headers={"Content-Type": "text/plain"})
        plain.body = plain.body.encode()
        router = Router(**{"cn.example": Resp(gbk.encode("gbk"), headers={"Content-Type": "text/html"}),
                           "pdf.example": Resp(b"%PDF-1.7", headers={"Content-Type": "application/pdf"}),
                           "zip.example": Resp(b"PK", headers={"Content-Type": "application/zip"}),
                           "txt.example": plain})
        ok = lambda url: {"cn.example": "1.1.1.1", "pdf.example": "1.1.1.1", "zip.example": "1.1.1.1", "txt.example": "1.1.1.1"}
        with dns(ok(None)):
            self.assertEqual(wb.fetch(url="https://cn.example/", open_=router)[0], "标题")
            with self.assertRaisesRegex(ValueError, "PDF"): wb.fetch(url="https://pdf.example/", open_=router)
            with self.assertRaisesRegex(ValueError, "unsupported"): wb.fetch(url="https://zip.example/", open_=router)
            title, body = wb.fetch(url="https://txt.example/", max_chars=1000, open_=router)
        self.assertTrue(body.startswith("x" * 1000 + "\n\n[showing the first 1000 of 9000 characters"))


class ToolWiringTests(unittest.TestCase):
    def ctx(self, reg, **extras):
        return ToolContext(policy=Policy(workspace=Path(".")), workspace=Path("."), extras={"registry": reg, **extras})

    def test_tools_are_visible_and_use_the_default_backends(self):
        reg = build_builtin_registry()
        visible = {s.name for s in reg.visible()}
        self.assertTrue({"web_search", "fetch_url"} <= visible)
        with patch.object(wb, "search", return_value=[{"title": "T", "url": "https://t.com", "snippet": "s",
                                                       "engine": "bing-us"}]) as search:
            result = reg.invoke("web_search", {"query": "t", "region": "global"}, self.ctx(reg))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.content, "1. T  [bing-us]\n   https://t.com\n   s")
        self.assertEqual(search.call_args.kwargs["region"], "global")
        with patch.object(wb, "fetch", return_value=("Title", "body\n\n[source: https://t.com]")):
            fetched = reg.invoke("fetch_url", {"url": "https://t.com"}, self.ctx(reg))
        self.assertEqual(fetched.content, "# Title\n\nbody\n\n[source: https://t.com]")
        host = reg.invoke("fetch_url", {"url": "https://t.com"},
                          self.ctx(reg, fetch_url=lambda url, max_chars: ("", "from host")))
        self.assertEqual(host.content, "from host")

    def test_gateway_tool_list_has_valid_parameter_schemas(self):
        from forge.gateway import GatewayConfig, serve
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            cfg = GatewayConfig(upstream="http://127.0.0.1:1", api_key="", port=0, gateway_token="", models=[],
                                registry=build_builtin_registry(), workspace=".", home=Path(home),
                                policy=Policy(workspace=Path("."), non_interactive=True))
            server = serve(cfg)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]
                tools = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/tools").read())["data"]
            finally:
                server.shutdown(); server.server_close()
        names = {t["function"]["name"] for t in tools}
        self.assertTrue({"forge_web_search", "forge_fetch_url", "forge_grep"} <= names)
        for tool in tools:
            for arg, shape in tool["function"]["parameters"]["properties"].items():
                self.assertIsInstance(shape["type"], str, f"{tool['function']['name']}.{arg}")
        grep = next(t for t in tools if t["function"]["name"] == "forge_grep")["function"]["parameters"]
        self.assertIn("regex", grep["properties"]["pattern"]["description"].lower())


if __name__ == "__main__":
    unittest.main()
