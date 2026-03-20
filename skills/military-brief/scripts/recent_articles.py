import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

LIST_URL = "https://nqsgsiyt.sealoshzh.site/list?page=1&size=15&k=AeO1IDE09i"
LOCAL_TZ = timezone(timedelta(hours=8))


def _build_headers(token: str | None) -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": "military-brief-recent-articles/1.0",
        "Accept": "*/*",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Token"] = token
        headers["Token"] = token
    return headers


def _http_get(url: str, headers: dict[str, str], timeout_s: int = 20) -> bytes:
    req = urllib.request.Request(url=url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def _try_json(data: bytes) -> object | None:
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_datetime(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _find_text(elem: ET.Element, names: list[str]) -> str:
    for name in names:
        child = elem.find(name)
        if child is not None and child.text:
            return child.text.strip()
    for child in list(elem):
        tag = child.tag
        local = tag.split("}", 1)[1] if "}" in tag else tag
        if local in names and child.text:
            return child.text.strip()
    return ""


def _extract_feeds_from_list_payload(payload: object) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    feeds: list[dict[str, str]] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        name = (it.get("name") if isinstance(it.get("name"), str) else "") or ""
        link = (it.get("link") if isinstance(it.get("link"), str) else "") or ""
        link = link.strip().strip("`").strip()
        if link:
            feeds.append({"name": name.strip() or link, "url": link})
    uniq: dict[str, dict[str, str]] = {}
    for f in feeds:
        uniq[f["url"]] = f
    return list(uniq.values())


def discover_feeds(list_url: str, token: str | None) -> list[dict[str, str]]:
    headers = _build_headers(token)
    raw = _http_get(list_url, headers=headers)
    payload = _try_json(raw)
    if payload is None:
        raise RuntimeError("list接口返回不是JSON，无法发现订阅链接")
    feeds = _extract_feeds_from_list_payload(payload)
    if not feeds:
        raise RuntimeError("list接口未解析出订阅链接，请检查返回字段是否包含 data[].link")
    return feeds


def _parse_rss_items(root: ET.Element, feed: dict[str, str]) -> list[dict]:
    channel = root.find("channel")
    if channel is None:
        channel = root.find("./{*}channel")
    if channel is None:
        return []
    items = channel.findall("item") or channel.findall("./{*}item") or []
    out: list[dict] = []
    for it in items:
        title = _find_text(it, ["title"]) or ""
        link = _find_text(it, ["link"]) or ""
        if not link:
            guid = _find_text(it, ["guid"]) or ""
            if guid.startswith("http://") or guid.startswith("https://"):
                link = guid
        link = (link or "").strip()
        if link and not urllib.parse.urlparse(link).scheme:
            link = urllib.parse.urljoin(feed["url"], link)
        desc = _find_text(it, ["description", "encoded"]) or ""
        pub = _find_text(it, ["pubDate", "date", "published", "updated"]) or ""
        out.append(
            {
                "title": title,
                "link": link,
                "summary": _strip_html(desc),
                "published": pub,
                "feed_name": feed["name"],
                "feed_url": feed["url"],
            }
        )
    return out


def _parse_atom_entries(root: ET.Element, feed: dict[str, str]) -> list[dict]:
    entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    if not entries:
        entries = root.findall("entry") or root.findall("./{*}entry") or []
    out: list[dict] = []
    for e in entries:
        title = _find_text(e, ["title"]) or ""
        link = ""
        for l in e.findall("{http://www.w3.org/2005/Atom}link") + e.findall("link") + e.findall("./{*}link"):
            href = l.attrib.get("href", "").strip()
            rel = (l.attrib.get("rel", "") or "").strip()
            if href and (not rel or rel == "alternate"):
                link = href
                break
        link = (link or "").strip()
        if link and not urllib.parse.urlparse(link).scheme:
            link = urllib.parse.urljoin(feed["url"], link)
        summary = _find_text(e, ["summary", "content"]) or ""
        pub = _find_text(e, ["published", "updated"]) or ""
        out.append(
            {
                "title": _strip_html(title),
                "link": link,
                "summary": _strip_html(summary),
                "published": pub,
                "feed_name": feed["name"],
                "feed_url": feed["url"],
            }
        )
    return out


def parse_feed(feed: dict[str, str], headers: dict[str, str]) -> list[dict]:
    raw = _http_get(feed["url"], headers=headers)
    try:
        root = ET.fromstring(raw)
    except Exception:
        text = raw.decode("utf-8", errors="ignore")
        root = ET.fromstring(text)
    tag = root.tag.split("}", 1)[-1].lower()
    if tag in ("rss", "rdf", "rdf:rdf"):
        return _parse_rss_items(root, feed)
    if tag == "feed":
        return _parse_atom_entries(root, feed)
    rss_items = _parse_rss_items(root, feed)
    if rss_items:
        return rss_items
    return _parse_atom_entries(root, feed)


def run_recent_articles(list_url: str, token: str | None, since_hours: int) -> dict:
    now = datetime.now(LOCAL_TZ)
    window_start = now - timedelta(hours=since_hours)
    feeds = discover_feeds(list_url=list_url, token=token)
    headers = _build_headers(token)

    raw_items: list[dict] = []
    for feed in feeds:
        try:
            raw_items.extend(parse_feed(feed, headers=headers))
        except Exception:
            continue

    articles: list[dict] = []
    for it in raw_items:
        published_dt = _parse_datetime(it.get("published", ""))
        if published_dt is None:
            continue
        published_local = published_dt.astimezone(LOCAL_TZ)
        if published_local < window_start:
            continue
        title = (it.get("title") or "").strip()
        link = (it.get("link") or "").strip()
        summary = (it.get("summary") or "").strip()
        if not title or not link:
            continue
        articles.append(
            {
                "title": title,
                "link": link,
                "published_at": published_local.isoformat(),
                "feed_name": it.get("feed_name") or it.get("feed_url") or "",
                "feed_url": it.get("feed_url") or "",
                "summary": summary[:400],
            }
        )

    articles.sort(key=lambda a: a.get("published_at", ""), reverse=True)
    return {
        "generated_at": now.isoformat(),
        "timezone": "+08:00",
        "base_url": list_url,
        "since_hours": since_hours,
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "feeds": feeds,
        "articles": articles,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="recent_articles.py")
    parser.add_argument("--since-hours", type=int, default=24)
    parser.add_argument("--out", default="-")
    args = parser.parse_args(argv)

    token = os.environ.get("WECHAT2RSS_TOKEN")
    result = run_recent_articles(list_url=LIST_URL, token=token, since_hours=args.since_hours)

    out_text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out == "-" or not args.out:
        sys.stdout.write(out_text + "\n")
        return 0
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out_text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
