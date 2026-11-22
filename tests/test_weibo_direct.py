# coding=utf-8
"""
微博桌面版直连 + RSSHub 主备双通道单元测试（离线 Mock，无外网依赖）
"""

from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
import os
import unittest

import pytz
import yaml

from trendradar.crawler.rss.fetcher import RSSFeedConfig, RSSFetcher
from trendradar.crawler.rss.weibo_direct import (
    WeiboAuthError,
    WeiboDirectError,
    _parse_weibo_search_time,
    build_weibo_ajax_headers,
    extract_weibo_keyword,
    extract_weibo_user_uid,
    fetch_weibo_keyword_parsed_items,
    fetch_weibo_user_parsed_items,
)


def _mock_response(
    status_code: int = 200,
    json_data=None,
    text: str = "",
    headers=None,
    url: str = "https://weibo.com/",
):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.url = url
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("not json")

    def _raise_for_status():
        if status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {status_code}")

    resp.raise_for_status.side_effect = _raise_for_status
    return resp


SAMPLE_RSSHUB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>CBA联赛官方微博</title>
    <item>
      <title>CBA常规赛最新赛程公告</title>
      <link>https://weibo.com/6351694466/FallbackPost1</link>
      <guid>https://weibo.com/6351694466/FallbackPost1</guid>
      <pubDate>Sun, 22 Nov 2026 01:00:00 +0800</pubDate>
      <author>CBA联赛</author>
      <description>CBA常规赛最新赛程公告</description>
    </item>
  </channel>
</rss>
"""


class TestWeiboDirectHelpers(unittest.TestCase):
    def test_route_extractors(self):
        self.assertEqual(
            extract_weibo_user_uid("https://rsshub.rss3.workers.dev/weibo/user/6351694466"),
            "6351694466",
        )
        self.assertIsNone(
            extract_weibo_user_uid("https://rsshub.rss3.workers.dev/weibo/keyword/CBA%20开票")
        )
        self.assertEqual(
            extract_weibo_keyword("https://rsshub.rss3.workers.dev/weibo/keyword/CBA%20%E5%BC%80%E7%A5%A8"),
            "CBA 开票",
        )
        self.assertEqual(
            extract_weibo_keyword("https://rsshub.rss3.workers.dev/weibo/keyword/大麦%20篮球"),
            "大麦 篮球",
        )
        self.assertIsNone(
            extract_weibo_keyword("https://openai.com/news/rss.xml")
        )

    def test_build_weibo_ajax_headers(self):
        headers = build_weibo_ajax_headers(
            "SUB=abc; XSRF-TOKEN=tok123; WBPSESS=xyz",
            uid="6351694466",
        )
        self.assertEqual(headers["X-XSRF-TOKEN"], "tok123")
        self.assertEqual(headers["Referer"], "https://weibo.com/u/6351694466")
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")
        self.assertIn("SUB=abc", headers["Cookie"])

    def test_parse_weibo_search_time(self):
        tz = pytz.timezone("Asia/Shanghai")
        now = tz.localize(datetime(2026, 11, 22, 12, 30, 0))

        self.assertEqual(_parse_weibo_search_time("刚刚", now), now.isoformat())
        self.assertEqual(
            _parse_weibo_search_time("15分钟前", now),
            (now - timedelta(minutes=15)).isoformat(),
        )
        self.assertEqual(
            _parse_weibo_search_time("今天 09:15", now),
            now.replace(hour=9, minute=15).isoformat(),
        )
        self.assertEqual(
            _parse_weibo_search_time("昨天 18:40", now),
            (now - timedelta(days=1)).replace(hour=18, minute=40).isoformat(),
        )
        self.assertEqual(
            _parse_weibo_search_time("11月21日 19:05", now),
            now.replace(month=11, day=21, hour=19, minute=5).isoformat(),
        )


class TestWeiboUserAjax(unittest.TestCase):
    def test_mymblog_filters_old_pinned_and_expands_longtext_and_retweet(self):
        tz = pytz.timezone("Asia/Shanghai")
        now = datetime.now(tz)
        old_dt = now - timedelta(days=90)
        fresh_dt = now - timedelta(hours=3)

        session = MagicMock()
        mymblog_payload = {
            "ok": 1,
            "data": {
                "list": [
                    {
                        "mblogid": "OldPinned1",
                        "isTop": 1,
                        "isLongText": True,
                        "created_at": format_datetime(old_dt),
                        "text_raw": "去年总决赛宣传置顶帖",
                        "user": {"screen_name": "CBA联赛"},
                    },
                    {
                        "mblogid": "FreshLong1",
                        "isLongText": True,
                        "created_at": format_datetime(fresh_dt),
                        "text_raw": "新赛季开票公告（截断）...",
                        "user": {"screen_name": "CBA联赛"},
                        "url_struct": [
                            {
                                "short_url": "http://t.cn/short1",
                                "ori_url": "https://detail.damai.cn/item.htm?id=123456",
                            }
                        ],
                        "retweeted_status": {
                            "mblogid": "RtOrig1",
                            "text_raw": "主场售票细则",
                            "user": {"screen_name": "大麦官博"},
                            "url_struct": [
                                {
                                    "ori_url": "https://m.piaoxingqiu.com/content/1?lssId=abc99"
                                }
                            ],
                        },
                    },
                ]
            },
        }
        longtext_payload = {
            "ok": 1,
            "data": {
                "longTextContent": "CBA联赛俱乐部杯正式开票，票价80元起，欢迎球迷购票观赛。"
            },
        }

        called_urls = []

        def _fake_get(url, **kwargs):
            called_urls.append(url)
            if "mymblog" in url:
                return _mock_response(200, json_data=mymblog_payload, url=url)
            if "longtext?id=FreshLong1" in url:
                return _mock_response(200, json_data=longtext_payload, url=url)
            raise AssertionError(f"Unexpected URL called: {url}")

        session.get.side_effect = _fake_get

        items = fetch_weibo_user_parsed_items(
            session=session,
            uid="6351694466",
            feed_name="CBA联赛官方微博",
            cookies="SUB=1; XSRF-TOKEN=t1",
            max_items=10,
            freshness_enabled=True,
            max_age_days=2,
            timezone="Asia/Shanghai",
        )

        # 陈旧置顶帖被前置过滤，绝不调用 OldPinned1 的 longtext
        self.assertEqual(len(items), 1)
        self.assertFalse(any("OldPinned1" in u for u in called_urls))

        item = items[0]
        self.assertEqual(item.url, "https://weibo.com/6351694466/FreshLong1")
        self.assertEqual(item.guid, "https://weibo.com/6351694466/FreshLong1")
        self.assertEqual(item.author, "CBA联赛")
        self.assertIn("CBA联赛俱乐部杯正式开票", item.title)
        self.assertIn("转发 @大麦官博: 主场售票细则", item.summary)
        # 展示用 summary 不应混入裸 URL，外链全部保留在 raw_summary 中
        self.assertNotIn("detail.damai.cn", item.summary)
        self.assertIn("https://detail.damai.cn/item.htm?id=123456", item.raw_summary)
        self.assertIn("https://m.piaoxingqiu.com/content/1?lssId=abc99", item.raw_summary)

    def test_mymblog_auth_expired_raises(self):
        session = MagicMock()
        session.get.return_value = _mock_response(
            200,
            json_data={"ok": -100, "url": "https://passport.weibo.com/sso/signin"},
            url="https://weibo.com/ajax/statuses/mymblog?uid=6351694466",
        )
        with self.assertRaises(WeiboAuthError):
            fetch_weibo_user_parsed_items(
                session=session,
                uid="6351694466",
                feed_name="CBA联赛官方微博",
                cookies="SUB=expired",
            )


class TestWeiboKeywordSearch(unittest.TestCase):
    def test_parse_search_cards_full_content_and_links(self):
        sample_html = """
        <html><body>
          <div class="card-wrap">无 mid 广告卡片</div>
          <div class="card-wrap" action-type="feed_list_item" mid="51017290001">
            <div class="content">
              <p class="txt" node-type="feed_list_content" nick-name="票务快讯">
                CBA俱乐部杯开票啦...
              </p>
              <p class="txt" node-type="feed_list_content_full" nick-name="票务快讯" style="display:none">
                CBA俱乐部杯正式开票！票价100元起，购票入口：
                <a href="https://detail.damai.cn/item.htm?id=778899">大麦购票</a>收起d
              </p>
              <div class="from">
                <a href="//weibo.com/1722647874/Qabc12345?refer_flag=1001030103_" target="_blank">
                  今天 10:20
                </a>
                来自 <a href="//app.weibo.com/">微博网页版</a>
              </div>
            </div>
          </div>
        </body></html>
        """
        session = MagicMock()
        session.get.return_value = _mock_response(
            200,
            text=sample_html,
            url="https://s.weibo.com/weibo?q=CBA%20%E5%BC%80%E7%A5%A8",
        )

        items = fetch_weibo_keyword_parsed_items(
            session=session,
            keyword="CBA 开票",
            cookies="SUB=valid",
            max_items=10,
            timezone="Asia/Shanghai",
        )

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.url, "https://weibo.com/1722647874/Qabc12345")
        self.assertEqual(item.guid, "https://weibo.com/1722647874/Qabc12345")
        self.assertEqual(item.author, "票务快讯")
        self.assertIn("票价100元起", item.title)
        self.assertFalse(item.title.endswith("收起d"))
        self.assertIn("https://detail.damai.cn/item.htm?id=778899", item.raw_summary)
        self.assertIsNotNone(item.published_at)

    def test_search_no_result_returns_empty_list(self):
        session = MagicMock()
        session.get.return_value = _mock_response(
            200,
            text="<html><body><div class='card-no-result'>抱歉，未找到相关结果。</div></body></html>",
            url="https://s.weibo.com/weibo?q=test",
        )
        items = fetch_weibo_keyword_parsed_items(
            session=session,
            keyword="test",
            cookies="SUB=valid",
        )
        self.assertEqual(items, [])

    def test_search_login_redirect_raises_auth_error(self):
        session = MagicMock()
        session.get.return_value = _mock_response(
            200,
            text="<html><title>Sina Visitor System</title></html>",
            url="https://passport.weibo.com/visitor/visitor",
        )
        with self.assertRaises(WeiboAuthError):
            fetch_weibo_keyword_parsed_items(
                session=session,
                keyword="CBA 开票",
                cookies="SUB=expired",
            )


class TestRSSFetcherDualChannelFailover(unittest.TestCase):
    def test_without_cookies_uses_rsshub_directly(self):
        feed = RSSFeedConfig(
            id="cba-official",
            name="CBA联赛官方微博",
            url="https://rsshub.rss3.workers.dev/weibo/user/6351694466",
            max_items=10,
        )
        with patch.dict(os.environ, {"WEIBO_COOKIES": ""}, clear=False):
            fetcher = RSSFetcher(feeds=[feed], request_interval=0)
            fetcher.session = MagicMock()
            fetcher.session.get.return_value = _mock_response(
                200,
                text=SAMPLE_RSSHUB_XML,
                headers={"Content-Type": "application/xml"},
                url=feed.url,
            )

            data = fetcher.fetch_all()
            self.assertEqual(data.failed_ids, [])
            self.assertEqual(len(data.items["cba-official"]), 1)
            self.assertEqual(fetcher._channel_stats["rsshub_fallback"], 1)
            self.assertEqual(fetcher._channel_stats["weibo_ajax"], 0)

    def test_auth_expired_auto_falls_back_to_rsshub_same_feed_id(self):
        feeds = [
            RSSFeedConfig(
                id="cba-official",
                name="CBA联赛官方微博",
                url="https://rsshub.rss3.workers.dev/weibo/user/6351694466",
                max_items=10,
            ),
            RSSFeedConfig(
                id="wcba-official",
                name="WCBA联赛官方微博",
                url="https://rsshub.rss3.workers.dev/weibo/user/6722173158",
                max_items=10,
            ),
        ]
        with patch.dict(os.environ, {"WEIBO_COOKIES": "SUB=expired"}, clear=False):
            fetcher = RSSFetcher(feeds=feeds, request_interval=0)
            fetcher.session = MagicMock()

            called = []

            def _fake_get(url, **kwargs):
                called.append(url)
                if "weibo.com/ajax" in url:
                    return _mock_response(
                        200,
                        json_data={"ok": -100, "url": "https://passport.weibo.com/"},
                        url=url,
                    )
                if "rsshub.rss3.workers.dev" in url:
                    return _mock_response(
                        200,
                        text=SAMPLE_RSSHUB_XML,
                        headers={"Content-Type": "application/xml"},
                        url=url,
                    )
                raise AssertionError(f"Unexpected URL: {url}")

            fetcher.session.get.side_effect = _fake_get
            data = fetcher.fetch_all()

            # 两个源均由 RSSHub 备通道兜底成功，且第二个源不再重复尝试失效的 weibo_ajax
            self.assertEqual(data.failed_ids, [])
            self.assertIn("cba-official", data.items)
            self.assertIn("wcba-official", data.items)
            self.assertTrue(fetcher._weibo_auth_expired)
            self.assertEqual(
                sum(1 for u in called if "weibo.com/ajax" in u),
                1,
            )
            self.assertEqual(fetcher._channel_stats["rsshub_fallback"], 2)

    def test_search_non_auth_circuit_break_does_not_affect_user_ajax(self):
        tz = pytz.timezone("Asia/Shanghai")
        fresh_dt = datetime.now(tz) - timedelta(hours=1)
        feeds = [
            RSSFeedConfig(
                id="ticket-cba-sale",
                name="票务专项｜CBA开票",
                url="https://rsshub.rss3.workers.dev/weibo/keyword/CBA%20开票",
                max_items=10,
                ticket_enrich=True,
            ),
            RSSFeedConfig(
                id="ticket-damai-basketball",
                name="票务专项｜大麦篮球",
                url="https://rsshub.rss3.workers.dev/weibo/keyword/大麦%20篮球",
                max_items=10,
                ticket_enrich=True,
            ),
            RSSFeedConfig(
                id="cba-official",
                name="CBA联赛官方微博",
                url="https://rsshub.rss3.workers.dev/weibo/user/6351694466",
                max_items=10,
            ),
        ]
        with patch.dict(os.environ, {"WEIBO_COOKIES": "SUB=ok; XSRF-TOKEN=t"}, clear=False):
            fetcher = RSSFetcher(feeds=feeds, request_interval=0)
            fetcher.session = MagicMock()

            def _fake_get(url, **kwargs):
                if "s.weibo.com" in url:
                    return _mock_response(500, text="server error", url=url)
                if "rsshub." in url:
                    return _mock_response(503, text="KV PUT failed: 429", url=url)
                if "weibo.com/ajax/statuses/mymblog" in url:
                    return _mock_response(
                        200,
                        json_data={
                            "ok": 1,
                            "data": {
                                "list": [
                                    {
                                        "mblogid": "OkAjax1",
                                        "created_at": format_datetime(fresh_dt),
                                        "text_raw": "CBA官微正常直连返回",
                                        "user": {"screen_name": "CBA联赛"},
                                    }
                                ]
                            },
                        },
                        url=url,
                    )
                raise AssertionError(f"Unexpected URL: {url}")

            fetcher.session.get.side_effect = _fake_get
            data = fetcher.fetch_all()

            # 即使 s.weibo.com 与 RSSHub 同时熔断，28 路官微直连依然正常工作
            self.assertEqual(data.failed_ids, ["ticket-cba-sale", "ticket-damai-basketball"])
            self.assertIn("cba-official", data.items)
            self.assertEqual(fetcher._channel_stats["weibo_ajax"], 1)
            self.assertTrue(fetcher._weibo_search_circuit_open)
            self.assertTrue(fetcher._rsshub_circuit_open)
            self.assertFalse(fetcher._weibo_ajax_circuit_open)

    def test_fixed_weibo_user_ticket_context_detects_basketball_in_long_body(self):
        feed = RSSFeedConfig(
            id="ticket-damai-official",
            name="大麦官方票务",
            url="https://rsshub.rss3.workers.dev/weibo/user/1722647874",
            ticket_enrich=True,
        )
        fetcher = RSSFetcher(feeds=[feed], request_interval=0)
        long_prefix = "周末演出与体育赛事重磅上新，多重优惠限时开启！" * 20
        full_body = f"{long_prefix} 2026-2027赛季CBA联赛北京首钢主场正式开票，票价80-680元。"
        self.assertTrue(
            fetcher._is_ticket_context(
                feed=feed,
                title=full_body[:300],
                summary=full_body[:500],
                raw_summary=full_body,
            )
        )


class TestConfigYamlIntegrity(unittest.TestCase):
    def test_config_yaml_weibo_routes_and_standalone_ids(self):
        config_path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
        with config_path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        feeds = cfg["rss"]["feeds"]
        enabled_feeds = [item for item in feeds if item.get("enabled", True)]
        self.assertEqual(len(enabled_feeds), 38)

        by_id = {item["id"]: item for item in feeds}
        ningbo = by_id["cba-ningbo"]
        self.assertEqual(
            ningbo["url"],
            "https://rsshub.rss3.workers.dev/weibo/user/7557035780",
        )
        self.assertNotIn("allowed_authors", ningbo)

        user_feeds = [
            item for item in enabled_feeds if extract_weibo_user_uid(item["url"])
        ]
        keyword_feeds = [
            item
            for item in enabled_feeds
            if not extract_weibo_user_uid(item["url"])
            and extract_weibo_keyword(item["url"])
        ]
        # 5 核心篮球官微 + 20 俱乐部 + 3 票务官微 = 28 路固定账号，5 路票务专项关键词
        self.assertEqual(len(user_feeds), 28)
        self.assertEqual(len(keyword_feeds), 5)

        standalone_ids = cfg["display"]["standalone"]["rss_feeds"]
        for sid in standalone_ids:
            self.assertIn(sid, by_id, f"standalone rss_feeds 包含不存在的 ID: {sid}")


if __name__ == "__main__":
    unittest.main()
