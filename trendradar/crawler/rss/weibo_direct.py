# coding=utf-8
"""
微博桌面版直连适配器（供 RSSFetcher 主备双通道使用）

支持两类微博 RSSHub 路由的原生直连（config.yaml 保留原 RSSHub 域名作为备通道）：
1. /weibo/user/<uid>    -> https://weibo.com/ajax/statuses/mymblog (+ longtext)
2. /weibo/keyword/<kw>  -> https://s.weibo.com/weibo?q=<kw>&typeall=1&suball=1
"""

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse, urlunparse
import html
import re

import requests

from .parser import ParsedRSSItem
from trendradar.utils.time import DEFAULT_TIMEZONE, get_configured_time, is_within_days


WEIBO_USER_ROUTE_RE = re.compile(r"/weibo/user/(\d+)(?:[/?#]|$)")
WEIBO_KEYWORD_ROUTE_RE = re.compile(r"/weibo/keyword/([^/?#]+)")

_LOGIN_URL_MARKERS = (
    "passport.weibo.com",
    "passport.weibo.cn",
    "login.sina.com.cn",
    "weibo.com/login",
)

_LOGIN_BODY_MARKERS = (
    "sina visitor system",
    "passport.weibo.com",
    "passport.weibo.cn",
    "login.sina.com.cn",
    "weibo.com/login",
)

_WEIBO_POST_URL_RE = re.compile(
    r"^(?:https?:)?//(?:www\.|m\.)?weibo\.com/(\d+)/([A-Za-z0-9]+)"
)


class WeiboDirectError(RuntimeError):
    """微博桌面版直连普通异常（网络、限流、结构变更等）"""


class WeiboAuthError(WeiboDirectError):
    """微博桌面版 Cookie 失效或未通过登录校验（WEIBO_AUTH_EXPIRED）"""


def extract_weibo_user_uid(url: str) -> Optional[str]:
    """从 /weibo/user/<uid> 路由中提取数字 UID。"""
    match = WEIBO_USER_ROUTE_RE.search(url or "")
    return match.group(1) if match else None


def extract_weibo_keyword(url: str) -> Optional[str]:
    """从 /weibo/keyword/<kw> 路由中提取解码后的搜索关键词。"""
    match = WEIBO_KEYWORD_ROUTE_RE.search(url or "")
    if not match:
        return None
    kw = unquote(match.group(1)).strip()
    return kw or None


def _extract_xsrf_token(cookies: str) -> str:
    for part in cookies.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip().upper() == "XSRF-TOKEN" and v.strip():
            return v.strip()
    return ""


def build_weibo_ajax_headers(cookies: str, uid: str = "") -> Dict[str, str]:
    """构造桌面版 weibo.com/ajax JSON 接口请求头。"""
    cleaned = cookies.strip()
    xsrf = _extract_xsrf_token(cleaned)
    referer = f"https://weibo.com/u/{uid}" if uid else "https://weibo.com/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": cleaned,
    }
    if xsrf:
        headers["X-XSRF-TOKEN"] = xsrf
    return headers


def build_weibo_search_headers(cookies: str) -> Dict[str, str]:
    """构造桌面版 s.weibo.com 搜索页请求头。"""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://s.weibo.com/",
        "Cookie": cookies.strip(),
    }


def _clean_inline_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def _normalize_weibo_post_url(raw_url: str) -> str:
    """规范化微博单帖 URL 为 https://weibo.com/{uid}/{mblogid}，确保跨日去重一致。"""
    url = (raw_url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    m = _WEIBO_POST_URL_RE.match(url)
    if m:
        return f"https://weibo.com/{m.group(1)}/{m.group(2)}"
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    return url


def _collect_status_links(status: Dict) -> List[str]:
    """从微博 JSON 的 url_struct / page_info 提取真实外链（优先 ori_url）。"""
    links: List[str] = []
    seen = set()

    def _add(candidate: Optional[str]) -> None:
        if not candidate or not isinstance(candidate, str):
            return
        u = candidate.strip()
        if not u.startswith(("http://", "https://")):
            return
        if u not in seen:
            seen.add(u)
            links.append(u)

    for entry in status.get("url_struct") or []:
        if not isinstance(entry, dict):
            continue
        _add(entry.get("ori_url"))
        _add(entry.get("short_url"))

    page_info = status.get("page_info")
    if isinstance(page_info, dict):
        _add(page_info.get("page_url"))
        _add(page_info.get("url_ori"))

    return links


def _fetch_longtext(
    session: requests.Session,
    mblogid: str,
    headers: Dict[str, str],
    timeout: int,
) -> Optional[str]:
    """展开折叠长微博全文；失败时静默返回 None，回退 text_raw。"""
    try:
        resp = session.get(
            f"https://weibo.com/ajax/statuses/longtext?id={mblogid}",
            headers=headers,
            timeout=(10, timeout),
            allow_redirects=False,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
        if payload.get("ok") == 1:
            content = (payload.get("data") or {}).get("longTextContent")
            if isinstance(content, str) and content.strip():
                return content.strip()
    except Exception:
        return None
    return None


def _check_auth_response(resp: requests.Response, scene: str) -> None:
    """检测 HTTP 401/403/418/432 或登录页跳转，归类为 WeiboAuthError。"""
    if resp.status_code in {401, 403, 418, 432}:
        raise WeiboAuthError(
            f"{scene} 返回 HTTP {resp.status_code} (WEIBO_AUTH_EXPIRED)"
        )

    location = (resp.headers.get("Location") or "").lower()
    final_url = (resp.url or "").lower()
    if any(marker in location or marker in final_url for marker in _LOGIN_URL_MARKERS):
        target = resp.headers.get("Location") or resp.url
        raise WeiboAuthError(
            f"{scene} 跳转至登录/访客验证页: {target} (WEIBO_AUTH_EXPIRED)"
        )


def fetch_weibo_user_parsed_items(
    session: requests.Session,
    uid: str,
    feed_name: str,
    cookies: str,
    timeout: int = 15,
    max_items: int = 0,
    freshness_enabled: bool = True,
    max_age_days: Optional[int] = None,
    timezone: str = DEFAULT_TIMEZONE,
    max_summary_length: int = 500,
) -> List[ParsedRSSItem]:
    """
    通过桌面版 weibo.com/ajax/statuses/mymblog 拉取官微时间线并转为 ParsedRSSItem。

    要点：
    - 先解析 created_at 并过滤超出 max_age_days 的旧帖（含长期置顶的 isTop=1 旧帖），
      通过后再调用 longtext，既省请求又避免挤占 max_items 配额；
    - title 保留前 300 字（不截断到 80 字，保护 ticket_enrich 篮球词识别）；
    - summary 保持干净纯文本，raw_summary 附加完整正文与 url_struct 真实外链。
    """
    headers = build_weibo_ajax_headers(cookies, uid=uid)
    api_url = f"https://weibo.com/ajax/statuses/mymblog?uid={uid}&page=1&feature=0"

    resp = session.get(
        api_url,
        headers=headers,
        timeout=(10, timeout),
        allow_redirects=False,
    )
    _check_auth_response(resp, f"weibo_ajax(uid={uid})")

    if resp.status_code != 200:
        raise WeiboDirectError(f"weibo_ajax(uid={uid}) 返回 HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except ValueError as exc:
        preview = (resp.text or "")[:2000].lower()
        if any(marker in preview for marker in _LOGIN_BODY_MARKERS):
            raise WeiboAuthError(
                f"weibo_ajax(uid={uid}) 返回登录页非 JSON (WEIBO_AUTH_EXPIRED)"
            ) from exc
        raise WeiboDirectError(f"weibo_ajax(uid={uid}) 响应非合法 JSON: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("ok") != 1:
        ok_val = payload.get("ok") if isinstance(payload, dict) else None
        payload_url = str(payload.get("url") or "").lower() if isinstance(payload, dict) else ""
        if ok_val in {-100, -200} or any(m in payload_url for m in _LOGIN_URL_MARKERS):
            raise WeiboAuthError(
                f"weibo_ajax(uid={uid}) 认证失效 ok={ok_val} (WEIBO_AUTH_EXPIRED)"
            )
        raise WeiboDirectError(f"weibo_ajax(uid={uid}) 接口异常 ok={ok_val}")

    statuses = (payload.get("data") or {}).get("list") or []
    items: List[ParsedRSSItem] = []

    for st in statuses:
        if not isinstance(st, dict):
            continue

        mblogid = str(st.get("mblogid") or st.get("idstr") or "").strip()
        if not mblogid:
            continue

        # 1. 解析发布时间并前置过滤超期帖/老置顶帖
        published_at: Optional[str] = None
        raw_created_at = st.get("created_at")
        if raw_created_at:
            try:
                published_at = parsedate_to_datetime(str(raw_created_at)).isoformat()
            except Exception:
                published_at = None

        if (
            freshness_enabled
            and max_age_days is not None
            and max_age_days > 0
            and published_at
            and not is_within_days(published_at, max_age_days, timezone)
        ):
            continue

        # 2. 正文与折叠长微博全文展开
        text = str(st.get("text_raw") or "").strip()
        if st.get("isLongText") is True:
            full_text = _fetch_longtext(session, mblogid, headers, timeout)
            if full_text:
                text = full_text

        # 3. 转发原帖拼接与外链汇总
        extra_links = _collect_status_links(st)
        retweeted = st.get("retweeted_status")
        if isinstance(retweeted, dict):
            rt_text = str(retweeted.get("text_raw") or "").strip()
            rt_mblogid = str(retweeted.get("mblogid") or retweeted.get("idstr") or "").strip()
            if retweeted.get("isLongText") is True and rt_mblogid:
                rt_full = _fetch_longtext(session, rt_mblogid, headers, timeout)
                if rt_full:
                    rt_text = rt_full
            rt_author = str(((retweeted.get("user") or {}).get("screen_name")) or "").strip()
            if rt_text:
                prefix = f"转发 @{rt_author}: " if rt_author else "转发: "
                text = f"{text}\n{prefix}{rt_text}" if text else f"{prefix}{rt_text}"
            for link in _collect_status_links(retweeted):
                if link not in extra_links:
                    extra_links.append(link)

        clean_text = _clean_inline_text(text)
        post_url = f"https://weibo.com/{uid}/{mblogid}"
        title = clean_text[:300] if clean_text else post_url
        summary = (
            clean_text[:max_summary_length] + "..."
            if len(clean_text) > max_summary_length
            else clean_text
        )
        raw_summary = (
            clean_text
            if not extra_links
            else f"{clean_text}\n" + "\n".join(extra_links)
        )
        author = str(((st.get("user") or {}).get("screen_name")) or feed_name).strip()

        items.append(
            ParsedRSSItem(
                title=title,
                url=post_url,
                published_at=published_at,
                summary=summary or None,
                author=author,
                guid=post_url,
                raw_summary=raw_summary[:20000] if raw_summary else None,
            )
        )

        if max_items > 0 and len(items) >= max_items:
            break

    return items


# ---------------------------------------------------------------------------
# 通道 2：s.weibo.com 桌面版关键词搜索直抓（5 路票务专项主通道）
# ---------------------------------------------------------------------------

def _parse_weibo_search_time(time_text: str, now: datetime) -> Optional[str]:
    """解析 s.weibo.com 卡片常见中文时间表达为 ISO 8601 字符串。"""
    s = _clean_inline_text(time_text)
    if not s:
        return None

    if "刚刚" in s:
        return now.isoformat()

    m = re.search(r"(\d+)\s*秒前", s)
    if m:
        return (now - timedelta(seconds=int(m.group(1)))).isoformat()

    m = re.search(r"(\d+)\s*分钟前", s)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).isoformat()

    m = re.search(r"今天\s*(\d{1,2}):(\d{2})", s)
    if m:
        return now.replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
        ).isoformat()

    m = re.search(r"昨天\s*(\d{1,2}):(\d{2})", s)
    if m:
        yesterday = now - timedelta(days=1)
        return yesterday.replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
        ).isoformat()

    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})", s)
    if m:
        try:
            return now.replace(
                year=int(m.group(1)),
                month=int(m.group(2)),
                day=int(m.group(3)),
                hour=int(m.group(4)),
                minute=int(m.group(5)),
                second=0,
                microsecond=0,
            ).isoformat()
        except ValueError:
            return None

    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})\s*(\d{1,2}):(\d{2})", s)
    if m:
        try:
            return now.replace(
                year=int(m.group(1)),
                month=int(m.group(2)),
                day=int(m.group(3)),
                hour=int(m.group(4)),
                minute=int(m.group(5)),
                second=0,
                microsecond=0,
            ).isoformat()
        except ValueError:
            return None

    m = re.search(r"(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?", s)
    if m:
        try:
            hour = int(m.group(3)) if m.group(3) is not None else 0
            minute = int(m.group(4)) if m.group(4) is not None else 0
            dt = now.replace(
                month=int(m.group(1)),
                day=int(m.group(2)),
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            if dt - now > timedelta(days=180):
                dt = dt.replace(year=dt.year - 1)
            return dt.isoformat()
        except ValueError:
            return None

    return None


class _WeiboSearchHTMLParser(HTMLParser):
    """解析 s.weibo.com 搜索结果页中带数字 mid 的真实微博卡片。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: List[Dict] = []

        self._in_card = False
        self._card_depth = 0
        self._current: Optional[Dict] = None

        self._capture_mode: Optional[str] = None  # "short" | "full" | "from" | "author"
        self._capture_depth = 0
        self._from_link_captured = False
        self._in_from_link = False
        self._from_link_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag_l = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        cls_tokens = set(attr_map.get("class", "").split())

        if not self._in_card:
            if tag_l == "div" and "card-wrap" in cls_tokens and attr_map.get("mid", "").isdigit():
                self._in_card = True
                self._card_depth = 1
                self._from_link_captured = False
                self._in_from_link = False
                self._from_link_depth = 0
                self._current = {
                    "mid": attr_map["mid"],
                    "author": "",
                    "short_parts": [],
                    "full_parts": [],
                    "links": [],
                    "post_url": "",
                    "time_parts": [],
                }
            return

        self._card_depth += 1
        assert self._current is not None

        if self._capture_mode is not None:
            self._capture_depth += 1
            if self._in_from_link:
                self._from_link_depth += 1
            if tag_l == "a":
                href = attr_map.get("href", "").strip()
                if self._capture_mode in {"short", "full"} and href:
                    if href.startswith("//"):
                        href = "https:" + href
                    if href.startswith(("http://", "https://")) and href not in self._current["links"]:
                        self._current["links"].append(href)
                elif self._capture_mode == "from" and not self._from_link_captured and href:
                    norm = _normalize_weibo_post_url(href)
                    if "weibo.com/" in norm:
                        self._current["post_url"] = norm
                        self._from_link_captured = True
                        self._in_from_link = True
                        self._from_link_depth = 1
            return

        node_type = attr_map.get("node-type", "")
        if tag_l == "p" and node_type in {"feed_list_content", "feed_list_content_full"}:
            if attr_map.get("nick-name") and not self._current["author"]:
                self._current["author"] = attr_map["nick-name"].strip()
            if node_type == "feed_list_content_full":
                if self._current["full_parts"]:
                    self._current["full_parts"].append(" ")
                self._capture_mode = "full"
            else:
                if self._current["short_parts"]:
                    self._current["short_parts"].append(" ")
                self._capture_mode = "short"
            self._capture_depth = 1
        elif tag_l in {"div", "p"} and "from" in cls_tokens and not self._from_link_captured:
            self._capture_mode = "from"
            self._capture_depth = 1
        elif tag_l == "a" and "name" in cls_tokens and not self._current["author"]:
            self._capture_mode = "author"
            self._capture_depth = 1

    def handle_data(self, data: str) -> None:
        if not self._in_card or self._current is None or not self._capture_mode:
            return
        if self._capture_mode == "short":
            self._current["short_parts"].append(data)
        elif self._capture_mode == "full":
            self._current["full_parts"].append(data)
        elif self._capture_mode == "from" and self._in_from_link:
            self._current["time_parts"].append(data)
        elif self._capture_mode == "author":
            self._current["author"] += data

    def handle_endtag(self, tag: str) -> None:
        if not self._in_card:
            return

        if self._in_from_link:
            self._from_link_depth -= 1
            if self._from_link_depth <= 0:
                self._in_from_link = False
                self._from_link_depth = 0

        if self._capture_mode is not None:
            self._capture_depth -= 1
            if self._capture_depth <= 0:
                if self._capture_mode == "author" and self._current is not None:
                    self._current["author"] = _clean_inline_text(self._current["author"])
                self._capture_mode = None
                self._capture_depth = 0

        self._card_depth -= 1
        if self._card_depth <= 0:
            if self._current is not None:
                self.cards.append(self._current)
            self._in_card = False
            self._card_depth = 0
            self._current = None


def fetch_weibo_keyword_parsed_items(
    session: requests.Session,
    keyword: str,
    cookies: str,
    timeout: int = 15,
    max_items: int = 0,
    timezone: str = DEFAULT_TIMEZONE,
    max_summary_length: int = 500,
) -> List[ParsedRSSItem]:
    """
    通过桌面版 s.weibo.com 搜索页拉取微博关键词结果并转为 ParsedRSSItem。

    - 命中登录页/访客验证页时抛出 WeiboAuthError 触发快速熔断并降级 RSSHub；
    - 优先取 feed_list_content_full 展开全文，并保留卡片外链到 raw_summary；
    - 搜索页明确提示无相关结果时返回空列表（不视为接口故障）。
    """
    headers = build_weibo_search_headers(cookies)
    search_url = (
        f"https://s.weibo.com/weibo?q={quote(keyword)}"
        "&typeall=1&suball=1&Refer=g"
    )

    resp = session.get(
        search_url,
        headers=headers,
        timeout=(10, timeout),
        allow_redirects=True,
    )
    _check_auth_response(resp, f"weibo_search(q={keyword})")

    if resp.status_code != 200:
        raise WeiboDirectError(
            f"weibo_search(q={keyword}) 返回 HTTP {resp.status_code}"
        )

    body = resp.text or ""
    body_lower = body[:5000].lower()
    if any(marker in body_lower for marker in _LOGIN_BODY_MARKERS):
        raise WeiboAuthError(
            f"weibo_search(q={keyword}) 页面要求登录/访客验证 (WEIBO_AUTH_EXPIRED)"
        )

    parser = _WeiboSearchHTMLParser()
    parser.feed(body)

    if not parser.cards:
        if any(
            marker in body
            for marker in ("抱歉，未找到", "没有找到相关结果", "card-no-result")
        ):
            return []
        if "card-wrap" not in body:
            raise WeiboDirectError(
                f"weibo_search(q={keyword}) 页面结构异常，未找到搜索结果容器"
            )
        return []

    now = get_configured_time(timezone)
    items: List[ParsedRSSItem] = []

    for card in parser.cards:
        raw_text_parts = card["full_parts"] or card["short_parts"]
        text = _clean_inline_text("".join(raw_text_parts))
        text = re.sub(r"(?:收起[a-z]?|展开[a-z]?)$", "", text).strip()
        if not text:
            continue

        post_url = card["post_url"] or f"https://weibo.com/detail/{card['mid']}"
        published_at = _parse_weibo_search_time("".join(card["time_parts"]), now)
        links = card["links"]
        summary = (
            text[:max_summary_length] + "..."
            if len(text) > max_summary_length
            else text
        )
        raw_summary = text if not links else f"{text}\n" + "\n".join(links)

        items.append(
            ParsedRSSItem(
                title=text[:300],
                url=post_url,
                published_at=published_at,
                summary=summary or None,
                author=card["author"] or None,
                guid=post_url,
                raw_summary=raw_summary[:20000] if raw_summary else None,
            )
        )

        if max_items > 0 and len(items) >= max_items:
            break

    return items
