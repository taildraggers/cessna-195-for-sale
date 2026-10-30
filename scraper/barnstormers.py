"""Scraper for Cessna 195 aircraft listings on barnstormers.com.

Listings come from a Barnstormers advanced search on the ad headline
"Cessna 195". Only whole-aircraft-for-sale listings are published: each ad's
title must name a Cessna 195 and not some other Cessna model, and
titles that look like parts/accessories/services/raffles are dropped.
Surviving titles are rewritten to a canonical "YEAR Cessna MODEL" form so
every listing follows the same format.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .common import (
    MIN_AIRCRAFT_PRICE,
    Listing,
    extract_date,
    extract_location,
    extract_price,
    fetch,
    format_aircraft_title,
    price_value,
)

SITE_NAME = "Barnstormers.com"
BASE = "https://www.barnstormers.com"
MAKE = "Cessna"

# Barnstormers advanced search for ads whose headline contains "Cessna 195".
SEARCH_URLS = [
    f"{BASE}/cat_search.php?headline=Cessna+195&body=&part_num=&mfg=&model="
    "&user__profile__company=&user__last_name=&user__first_name="
    "&user__profile__country=&specialcase__state=&user__profile__city="
    "&user__profile__uzip=&specialcase__phone=&user__email=&my_cats__name="
    "&price__gte=&price__lte=&search_type=advanced&keyword=",
]

MAX_PAGES = 15
LISTING_LINK_RE = re.compile(r"^/classified-(\d+)-(.+)\.html$")
GENERIC_SITE_TITLE_SNIPPET = "barnstormers.com find aircraft"

# "195", optionally prefixed "C-"/"C" and suffixed with its variant letter
# (195A, 195B, or "195 B" spaced out - though not a spaced "A", which is far
# more often just the next word), but not "195 HP". The LC-126 is the 195's
# military version.
_HP_SUFFIX = r"(?!\s?-?\s?(?:hp|h\.p|horse))"
_MODEL_RE = re.compile(
    r"(?<![\d.])(?:c\s?-?\s?)?195(?:([ab])|\s?-?\s?(b))?(?!\d)(?![a-z])" + _HP_SUFFIX,
    re.IGNORECASE,
)
_LC126_RE = re.compile(r"\blc\s?-?\s?126([a-c])?(?![\da-z])", re.IGNORECASE)
# Other Cessna models. A title naming one of these is some other airplane
# (including the 190, the 195's sibling - so a "190/195" ad is dropped too)
# or a mixed parts ad - except as an engine rating ("300 HP").
_OTHER_MODEL_RE = re.compile(
    r"(?<![\d.])(?:a\s?)?(?:120|140|150|152|170|172|175|177|180|182|185|190|205|206|207|210)(?!\d)"
    + _HP_SUFFIX,
    re.IGNORECASE,
)


def _extract_model(title: str) -> tuple[str, str] | None:
    if _OTHER_MODEL_RE.search(title):
        return None
    match = _MODEL_RE.search(title)
    if match:
        return MAKE, "195" + (match.group(1) or match.group(2) or "").upper()
    match = _LC126_RE.search(title)
    if match:
        return MAKE, "LC-126" + (match.group(1) or "").upper()
    return None


def _title_from_url(url: str) -> str:
    """Listing pages share a generic <title>/<h1>, but the URL slug is the ad's own title."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    match = LISTING_LINK_RE.match("/" + slug)
    if not match:
        return unquote(slug)
    return unquote(match.group(2)).replace("-", " ").strip()


def _find_listing_links(html: str) -> set[str]:
    soup = BeautifulSoup(html, "lxml")
    links = set()
    for a in soup.find_all("a", href=True):
        href = urlparse(urljoin(BASE, a["href"])).path
        if LISTING_LINK_RE.match(href):
            links.add(urljoin(BASE, href))
    return links


def _page_url(search_url: str, page: int) -> str:
    """Build a search results page's URL directly by setting its page=<n>
    query parameter, rather than hunting for "Next" links - Barnstormers'
    pager renders as bare page-number buttons with no "Next" text or
    rel="next" for a link-following heuristic to find.
    """
    if page <= 1:
        return search_url
    parts = urlparse(search_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
    query.append(("page", str(page)))
    return urlunparse(parts._replace(query=urlencode(query)))


def _debug_dump_hrefs(html: str, limit: int = 25) -> None:
    soup = BeautifulSoup(html, "lxml")
    hrefs = [a["href"] for a in soup.find_all("a", href=True)]
    interesting = [h for h in hrefs if "classified" in h.lower() or "cessna" in h.lower()]
    sample = interesting[:limit] or hrefs[:limit]
    print(f"  [debug] {len(hrefs)} total <a href> on page; sample: {sample}")


def _parse_detail_page(url: str, html: str) -> Listing | None:
    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None
    if title:
        title = re.sub(r"\s*[\|\-]\s*Barnstormers.*$", "", title, flags=re.IGNORECASE).strip()
    if not title or GENERIC_SITE_TITLE_SNIPPET in title.lower():
        title = _title_from_url(url)
    if not title:
        return None

    text = soup.get_text(" ", strip=True)

    formatted_title = format_aircraft_title(title, text, _extract_model)
    if not formatted_title:
        return None
    title = formatted_title

    price = extract_price(text)
    value = price_value(price)
    if value is not None and value < MIN_AIRCRAFT_PRICE:
        return None
    location = extract_location(text)
    date_posted = extract_date(text)

    return Listing(
        title=title,
        price=price,
        location=location,
        date_posted=date_posted,
        site=SITE_NAME,
        url=url,
    )


def scrape() -> list[Listing]:
    print(f"[{SITE_NAME}] starting scrape")
    all_links: set[str] = set()

    for search_url in SEARCH_URLS:
        seen_this_search: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            url = _page_url(search_url, page)
            html = fetch(url)
            if not html:
                break
            links = _find_listing_links(html)
            new_links = links - seen_this_search
            print(f"  [search page {page}]: {len(links)} links ({len(new_links)} new)")
            if page == 1 and not links:
                _debug_dump_hrefs(html)
            seen_this_search |= links
            if not new_links:
                break
        all_links |= seen_this_search

    print(f"[{SITE_NAME}] {len(all_links)} unique listing URLs found")

    listings: list[Listing] = []
    for url in sorted(all_links):
        html = fetch(url)
        if not html:
            continue
        listing = _parse_detail_page(url, html)
        if listing:
            listings.append(listing)

    print(f"[{SITE_NAME}] parsed {len(listings)} listings")
    return listings
