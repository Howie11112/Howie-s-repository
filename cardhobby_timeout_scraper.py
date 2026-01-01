#!/usr/bin/env python3
"""Scrape CardHobby timeout auctions and filter by price/time/keywords.

Example:
  python cardhobby_timeout_scraper.py --max-price 300 --hours 5 --output listings.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html import unescape
from typing import Any, Iterable, Optional


BASE_URL = "https://www.cardhobby.com.cn/market/timeout"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"
DEFAULT_KEYWORDS = [
    "签名",
    "签字",
    "物料",
    "patch",
    "编号",
    "限量",
    "auto",
    "autograph",
    "signed",
    "serial",
    "numbered",
    "limited",
]
KEYWORD_OPTIONS = {
    "签名": ["签名", "签字", "auto", "autograph", "signed"],
    "物料": ["物料", "patch"],
    "编号": ["编号", "限量", "serial", "numbered", "limited"],
}
TIME_OPTIONS = [3, 6, 12]


@dataclass
class Listing:
    title: str
    price: Optional[float]
    end_time: Optional[dt.datetime]
    url: Optional[str]
    source: str


def build_opener(proxy: Optional[str], no_proxy: bool) -> Optional[urllib.request.OpenerDirector]:
    if no_proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return None


def fetch_url(
    url: str,
    headers: dict[str, str],
    opener: Optional[urllib.request.OpenerDirector],
) -> str:
    request = urllib.request.Request(url, headers=headers)
    open_func = opener.open if opener else urllib.request.urlopen
    with open_func(request, timeout=30) as response:
        data = response.read()
    return data.decode("utf-8", errors="replace")


def extract_embedded_json(html: str) -> list[Any]:
    candidates: list[Any] = []
    script_pattern = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
    for match in script_pattern.finditer(html):
        content = match.group(1).strip()
        if not content:
            continue
        if "{" not in content and "[" not in content:
            continue
        json_candidate = None
        if "__NEXT_DATA__" in content or "application/json" in match.group(0).lower():
            json_candidate = content
        elif "window.__NUXT__" in content:
            nuxt_match = re.search(r"window\.__NUXT__\s*=\s*(\{.*?\});", content, re.DOTALL)
            if nuxt_match:
                json_candidate = nuxt_match.group(1)
        if json_candidate is None:
            continue
        json_candidate = json_candidate.strip().rstrip(";")
        try:
            candidates.append(json.loads(json_candidate))
        except json.JSONDecodeError:
            continue
    return candidates


def walk_values(obj: Any) -> Iterable[Any]:
    if isinstance(obj, dict):
        for value in obj.values():
            yield value
            yield from walk_values(value)
    elif isinstance(obj, list):
        for item in obj:
            yield item
            yield from walk_values(item)


def find_listing_dicts(data: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            keys = {str(key).lower() for key in obj.keys()}
            if any("title" in key or "name" in key for key in keys) and any(
                "price" in key or "current" in key for key in keys
            ):
                if any(
                    "end" in key
                    or "expire" in key
                    or "timeout" in key
                    or "deadline" in key
                    for key in keys
                ):
                    results.append(obj)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return results


def first_value(obj: dict[str, Any], *keywords: str) -> Optional[Any]:
    for key, value in obj.items():
        lowered = str(key).lower()
        if any(keyword in lowered for keyword in keywords):
            return value
    return None


def parse_price(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_end_time(value: Any, now: dt.datetime) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000
        if ts > 1e9:
            return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone()
        return None
    text = str(value)
    text = text.strip()
    if not text:
        return None
    datetime_patterns = [
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
    ]
    for fmt in datetime_patterns:
        try:
            parsed = dt.datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=now.tzinfo)
        except ValueError:
            continue
    hms_match = re.search(r"(\d+):(\d+):(\d+)", text)
    if hms_match:
        hours, minutes, seconds = map(int, hms_match.groups())
        return now + dt.timedelta(hours=hours, minutes=minutes, seconds=seconds)
    hour_match = re.search(r"(\d+)\s*小时", text)
    minute_match = re.search(r"(\d+)\s*分钟", text)
    if hour_match or minute_match:
        hours = int(hour_match.group(1)) if hour_match else 0
        minutes = int(minute_match.group(1)) if minute_match else 0
        return now + dt.timedelta(hours=hours, minutes=minutes)
    return None


def normalize_listing(raw: dict[str, Any], source: str, base_url: str) -> Listing:
    title = first_value(raw, "title", "name")
    title_text = unescape(str(title)) if title is not None else ""
    price_value = first_value(raw, "price", "current")
    end_value = first_value(raw, "end", "expire", "timeout", "deadline")
    url_value = first_value(raw, "url", "link", "href")
    url_text = None
    if url_value:
        url_text = str(url_value)
        if url_text.startswith("/"):
            url_text = urllib.parse.urljoin(base_url, url_text)
    return Listing(
        title=title_text,
        price=parse_price(price_value),
        end_time=parse_end_time(end_value, dt.datetime.now().astimezone()),
        url=url_text,
        source=source,
    )


def collect_listings(html: str, base_url: str) -> list[Listing]:
    listings: list[Listing] = []
    for payload in extract_embedded_json(html):
        for raw in find_listing_dicts(payload):
            listings.append(normalize_listing(raw, "embedded_json", base_url))
    return listings


def filter_listings(
    listings: Iterable[Listing],
    max_price: float,
    deadline: dt.datetime,
    keywords: list[str],
) -> list[Listing]:
    filtered: list[Listing] = []
    for listing in listings:
        if not listing.title:
            continue
        lowered = listing.title.lower()
        if not any(keyword.lower() in lowered for keyword in keywords):
            continue
        if listing.price is None or listing.price > max_price:
            continue
        if listing.end_time is None or listing.end_time > deadline:
            continue
        filtered.append(listing)
    return filtered


def write_csv(path: str, listings: Iterable[Listing]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["title", "price", "end_time", "url", "source"])
        for listing in listings:
            end_time = listing.end_time.isoformat() if listing.end_time else ""
            writer.writerow([listing.title, listing.price, end_time, listing.url, listing.source])


def build_page_url(base_url: str, page: int, page_param: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    query[page_param] = str(page)
    new_query = urllib.parse.urlencode(query)
    return urllib.parse.urlunparse(parsed._replace(query=new_query))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Filter CardHobby timeout auctions.")
    parser.add_argument("--max-price", type=float, required=True, help="Max price (integer).")
    parser.add_argument("--hours", type=float, default=5, help="Time window in hours.")
    parser.add_argument("--output", default="listings.csv", help="Output CSV path.")
    parser.add_argument("--keywords", nargs="*", default=DEFAULT_KEYWORDS)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--page-count", type=int, default=1, help="How many pages to fetch.")
    parser.add_argument("--page-param", default="page", help="Pagination query param.")
    parser.add_argument("--proxy", help="Proxy URL, e.g. http://127.0.0.1:7890.")
    parser.add_argument("--no-proxy", action="store_true", help="Ignore proxy env vars.")
    parser.add_argument("--cookie", help="Raw cookie string to send with requests.")
    return parser.parse_args(argv)


def run_scrape(
    max_price: float,
    hours: float,
    keywords: list[str],
    base_url: str,
    page_count: int,
    page_param: str,
    proxy: Optional[str],
    no_proxy: bool,
    cookie: Optional[str],
) -> tuple[list[Listing], list[str]]:
    now = dt.datetime.now().astimezone()
    deadline = now + dt.timedelta(hours=hours)
    listings: list[Listing] = []
    errors: list[str] = []
    opener = build_opener(proxy, no_proxy)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": base_url,
    }
    if cookie:
        headers["Cookie"] = cookie

    for page in range(1, page_count + 1):
        page_url = build_page_url(base_url, page, page_param)
        try:
            html = fetch_url(page_url, headers, opener)
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                errors.append(
                    "403 Forbidden. The site or proxy blocked the request. "
                    "Try --no-proxy (if your network allows direct access) "
                    "or supply --proxy/--cookie."
                )
            errors.append(f"Failed to fetch {page_url}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - provide readable output for users
            errors.append(f"Failed to fetch {page_url}: {exc}")
            continue
        listings.extend(collect_listings(html, base_url))

    if not listings:
        errors.append("No listings detected. The page may require JS or selectors updates.")
        return [], errors

    filtered = filter_listings(listings, max_price, deadline, keywords)
    filtered.sort(key=lambda item: item.end_time or dt.datetime.max.replace(tzinfo=now.tzinfo))
    return filtered, errors


def write_listings_text(listings: Iterable[Listing]) -> str:
    lines: list[str] = []
    for listing in listings:
        end_time = listing.end_time.isoformat() if listing.end_time else ""
        lines.append(f"{listing.title} | {listing.price} | {end_time} | {listing.url or ''}")
    return "\n".join(lines)


def launch_gui() -> int:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext

    root = tk.Tk()
    root.title("CardHobby Timeout 筛选")

    options_frame = tk.LabelFrame(root, text="卡片类型")
    options_frame.pack(fill="x", padx=10, pady=8)

    keyword_vars: dict[str, tk.BooleanVar] = {}
    for idx, label in enumerate(KEYWORD_OPTIONS):
        var = tk.BooleanVar(value=True)
        keyword_vars[label] = var
        tk.Checkbutton(options_frame, text=label, variable=var).grid(
            row=0, column=idx, padx=6, pady=4, sticky="w"
        )

    time_frame = tk.LabelFrame(root, text="时间窗口")
    time_frame.pack(fill="x", padx=10, pady=8)
    time_var = tk.IntVar(value=TIME_OPTIONS[0])
    for idx, hours in enumerate(TIME_OPTIONS):
        tk.Radiobutton(
            time_frame,
            text=f"{hours} 小时内",
            variable=time_var,
            value=hours,
        ).grid(row=0, column=idx, padx=6, pady=4, sticky="w")

    price_frame = tk.Frame(root)
    price_frame.pack(fill="x", padx=10, pady=8)
    tk.Label(price_frame, text="最高价格:").pack(side="left")
    price_entry = tk.Entry(price_frame, width=12)
    price_entry.insert(0, "300")
    price_entry.pack(side="left", padx=6)

    output_frame = tk.Frame(root)
    output_frame.pack(fill="both", expand=True, padx=10, pady=8)
    output_text = scrolledtext.ScrolledText(output_frame, height=14)
    output_text.pack(fill="both", expand=True)

    def on_run() -> None:
        output_text.delete("1.0", tk.END)
        try:
            max_price = float(price_entry.get().strip())
        except ValueError:
            messagebox.showerror("输入错误", "请输入数字价格。")
            return

        selected_keywords: list[str] = []
        for label, var in keyword_vars.items():
            if var.get():
                selected_keywords.extend(KEYWORD_OPTIONS[label])
        if not selected_keywords:
            messagebox.showwarning("提示", "请至少选择一个关键词类型。")
            return

        listings, errors = run_scrape(
            max_price=max_price,
            hours=float(time_var.get()),
            keywords=selected_keywords,
            base_url=BASE_URL,
            page_count=1,
            page_param="page",
            proxy=None,
            no_proxy=False,
            cookie=None,
        )

        if errors:
            output_text.insert(tk.END, "\n".join(errors) + "\n\n")
        if listings:
            output_text.insert(tk.END, write_listings_text(listings))
        else:
            output_text.insert(tk.END, "没有找到符合条件的结果。")

    tk.Button(root, text="开始筛选", command=on_run).pack(pady=6)
    root.mainloop()
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        return launch_gui()

    args = parse_args(argv)
    listings, errors = run_scrape(
        max_price=args.max_price,
        hours=args.hours,
        keywords=list(args.keywords),
        base_url=args.base_url,
        page_count=args.page_count,
        page_param=args.page_param,
        proxy=args.proxy,
        no_proxy=args.no_proxy,
        cookie=args.cookie,
    )
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
    if not listings:
        return 1
    write_csv(args.output, listings)
    print(f"Saved {len(listings)} listings to {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
