"""
News-Aggregator fuer die Scanner-Startseite. Holt mehrere RSS/Atom-Feeds
(Krypto + Maerkte/Aktien), parst mit der stdlib (kein feedparser noetig) und
liefert die juengsten Schlagzeilen als Liste zurueck.

Robust: jeder Feed einzeln in try/except; faellt einer aus (Cloud-IP geblockt,
Timeout), liefern die anderen weiter. Komplett ohne API-Key.

ENV:
  NEWS_FEEDS  -- optionale Komma-Liste "Quelle|URL,Quelle|URL" ueberschreibt die Defaults
  NEWS_MAX    -- max. Schlagzeilen (default 28)
  NEWS_PER_SRC -- max. Schlagzeilen je Quelle (default 7), damit ein prolifischer
                 Feed (Yahoo) die anderen nicht verdraengt -> ausgewogener Mix
  NEWS_BLOCK   -- optionale Komma-Liste zusaetzlicher Regex (case-insensitive), die
                 Titel ausfiltern (Default-Blocklist gegen Yahoo-Evergreen/Cramer)
"""
import os, re, datetime
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# (Quelle, URL) -- keyfrei, cloud-tauglich. Mix aus Krypto + Maerkten/Aktien,
# passend zu den beiden Scannern (Small-Cap-Gapper + Krypto-Perps).
DEFAULT_FEEDS = [
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("CNBC Finance",  "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
    ("MarketWatch",   "http://feeds.marketwatch.com/marketwatch/bulletins"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
]

NS = {"atom": "http://www.w3.org/2005/Atom"}

# Rausch-Blocklist (v.a. Yahoos Content-Mill): taeglich neu generierte Zins-/Spar-
# tabellen, Cramer-Meinungs-Roundups, Penny-Stock-Listicles, Personal-Finance-Fluff.
# Diese verdraengen sonst die echten Katalysator-Schlagzeilen. Regex, case-insensitive.
DEFAULT_BLOCK = [
    r"jim cramer",
    r"penny stocks?",
    r"\bheloc\b",
    r"\bapy\b",
    r"home equity (loan|line)",
    r"mortgage (and |&\s*)?(refinance )?(interest )?rates?",
    r"refinance rates?",
    r"\b(cd|savings|money market|high[- ]yield)\b.*\brates?\b",
    r"\b401\(?k\)?\b",
    r"\bhigh[- ]yield savings\b",
]


def _blocklist():
    pats = list(DEFAULT_BLOCK)
    extra = os.getenv("NEWS_BLOCK", "").strip()
    if extra:
        pats += [p.strip() for p in extra.split(",") if p.strip()]
    return [re.compile(p, re.IGNORECASE) for p in pats]


def _feeds():
    raw = os.getenv("NEWS_FEEDS", "").strip()
    if not raw:
        return DEFAULT_FEEDS
    out = []
    for part in raw.split(","):
        if "|" in part:
            src, url = part.split("|", 1)
            out.append((src.strip(), url.strip()))
    return out or DEFAULT_FEEDS


def _strip_ns(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def _parse_date(s):
    """RSS (RFC822) ODER Atom (ISO8601) -> aware datetime (UTC). None bei Fehlschlag."""
    if not s:
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _parse_feed(source, xml_bytes):
    """RSS 2.0 (<item>) und Atom (<entry>) -> Liste von Eintraegen."""
    items = []
    root = ET.fromstring(xml_bytes)
    # RSS: channel/item ; Atom: feed/entry
    nodes = [e for e in root.iter() if _strip_ns(e.tag) in ("item", "entry")]
    for n in nodes:
        title = link = date = ""
        for ch in n:
            t = _strip_ns(ch.tag)
            if t == "title":
                title = _text(ch)
            elif t == "link":
                # RSS: Text im link; Atom: href-Attribut (bevorzugt rel="alternate")
                href = ch.get("href")
                if href and (ch.get("rel") in (None, "alternate")):
                    link = href
                elif _text(ch):
                    link = _text(ch)
            elif t in ("pubDate", "published", "updated", "date") and not date:
                date = _text(ch)
        title = re.sub(r"<[^>]+>", "", title).strip()
        if title and link:
            items.append({"source": source, "title": title, "link": link,
                          "dt": _parse_date(date)})
    return items


def fetch_news(max_items=None, timeout=12):
    max_items = int(os.getenv("NEWS_MAX", str(max_items or 28)))
    per_src = int(os.getenv("NEWS_PER_SRC", "7"))
    block = _blocklist()
    far_past = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    by_src, seen = {}, set()
    for source, url in _feeds():
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
                             timeout=timeout)
            r.raise_for_status()
            got = []; dropped = 0
            for it in _parse_feed(source, r.content):
                key = it["title"].lower()[:80]
                if key in seen:
                    continue
                if any(p.search(it["title"]) for p in block):   # Rausch-Filter
                    dropped += 1
                    continue
                seen.add(key)
                got.append(it)
            # je Quelle nur die juengsten per_src -> kein Feed verdraengt die anderen
            got.sort(key=lambda x: x["dt"] or far_past, reverse=True)
            by_src[source] = got[:per_src]
            print(f"[news] {source}: ok ({len(got[:per_src])}, {dropped} gefiltert)")
        except Exception as e:
            print(f"[news] {source}: FEHLER {e}")
    all_items = [it for items in by_src.values() for it in items]
    all_items.sort(key=lambda x: x["dt"] or far_past, reverse=True)
    return all_items[:max_items]


if __name__ == "__main__":
    for it in fetch_news():
        when = it["dt"].strftime("%Y-%m-%d %H:%M") if it["dt"] else "?"
        print(f"{when}  [{it['source']}]  {it['title']}")
