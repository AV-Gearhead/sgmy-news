"""
Fetch Singapore + Malaysia news from trusted sources, label each story,
and save everything to news.json for the webpage to read.

Run it yourself with:   python fetch_news.py
GitHub runs it automatically every 30 minutes (see .github/workflows/update.yml).
"""

import hashlib
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser

HERE = Path(__file__).parent
FEEDS_FILE = HERE / "feeds.json"
KEYWORDS_FILE = HERE / "keywords.json"
OUTPUT_FILE = HERE / "news.json"

KEEP_HOURS = 72          # how long a story stays on the page
MAX_ITEMS = 800          # safety cap on file size
SNIPPET_CHARS = 160      # keep snippets short; readers click through to the source
USER_AGENT = "Mozilla/5.0 (compatible; SGMY-NewsWatch/1.0; personal news reader)"

# Google News search editions for each language
GOOGLE_EDITIONS = {
    "en": {"hl": "en-SG", "gl": "SG", "ceid": "SG:en"},
    "ms": {"hl": "ms", "gl": "MY", "ceid": "MY:ms"},
    "zh": {"hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
}


# ---------- helpers ----------

def load_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def google_news_url(domain, lang):
    params = dict(GOOGLE_EDITIONS.get(lang, GOOGLE_EDITIONS["en"]))
    params["q"] = f"site:{domain} when:2d"
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def download(url):
    """Return feed bytes. Local file paths are allowed for testing."""
    if not url.startswith("http"):
        return Path(url).read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read()


def clean_text(raw):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def strip_source_suffix(title, source_name):
    """Google News titles end with ' - Publisher'. Remove that tail."""
    if " - " in title:
        head, tail = title.rsplit(" - ", 1)
        if len(tail) <= 50 and head:
            return head.strip()
    return title


def normalise_for_dedupe(title):
    return re.sub(r"[\W_]+", "", title.lower())


def story_time(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        return datetime(*t[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


# ---------- classification ----------

class Classifier:
    def __init__(self, keywords):
        self.ignore = self._compile(keywords.get("ignore", {}))
        self.fatal = self._compile(keywords.get("fatal", {}))
        self.major = self._compile(keywords.get("major", {}))

    @staticmethod
    def _compile(by_lang):
        """English/Malay: whole-word match. Chinese: match anywhere."""
        patterns = []
        for lang, words in by_lang.items():
            for w in words:
                w = w.strip()
                if not w:
                    continue
                if lang == "zh":
                    patterns.append(re.escape(w))
                else:
                    patterns.append(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])")
        if not patterns:
            return None
        return re.compile("|".join(patterns), re.IGNORECASE)

    def classify(self, text):
        if self.ignore:
            text = self.ignore.sub(" ", text)
        if self.fatal and self.fatal.search(text):
            return "fatal"
        if self.major and self.major.search(text):
            return "major"
        return "other"


# ---------- main ----------

def fetch_source(src):
    url = src.get("rss") or google_news_url(src["domain"], src.get("lang", "en"))
    via_google = "rss" not in src
    parsed = feedparser.parse(download(url))
    stories = []
    for e in parsed.entries:
        title = clean_text(e.get("title", ""))
        link = e.get("link", "")
        if not title or not link:
            continue
        if via_google:
            title = strip_source_suffix(title, src["name"])
            snippet = ""  # Google's summary is just the headline again
        else:
            snippet = clean_text(e.get("summary", ""))
            if len(snippet) > SNIPPET_CHARS:
                snippet = snippet[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "…"
        stories.append({
            "title": title,
            "link": link,
            "snippet": snippet,
            "published": story_time(e).isoformat(),
        })
    return stories


def main():
    config = load_json(FEEDS_FILE)
    keywords = load_json(KEYWORDS_FILE, {})
    if not config:
        sys.exit("feeds.json is missing or has a typo. Check commas and quotes.")
    classifier = Classifier(keywords)

    previous = load_json(OUTPUT_FILE, {}) or {}
    stories = {s["id"]: s for s in previous.get("stories", [])}
    health = []

    for src in config["sources"]:
        if not src.get("enabled", True):
            continue
        try:
            fetched = fetch_source(src)
            health.append({"name": src["name"], "ok": True, "count": len(fetched)})
            print(f"  ok   {len(fetched):>3}  {src['name']}")
        except Exception as exc:  # one broken source should never stop the rest
            health.append({"name": src["name"], "ok": False, "count": 0, "error": str(exc)[:120]})
            print(f"  FAIL      {src['name']}: {exc}")
            continue

        for s in fetched:
            sid = hashlib.sha1(s["link"].encode()).hexdigest()[:16]
            text = f"{s['title']} {s['snippet']}"
            stories[sid] = {
                "id": sid,
                **s,
                "source": src["name"],
                "country": src["country"],
                "group": src["group"],
                "lang": src.get("lang", "en"),
                "severity": classifier.classify(text),
            }

    # Drop old stories, then drop duplicate headlines (keep the earliest copy).
    cutoff = datetime.now(timezone.utc) - timedelta(hours=KEEP_HOURS)
    fresh = [s for s in stories.values() if datetime.fromisoformat(s["published"]) >= cutoff]
    fresh.sort(key=lambda s: s["published"])
    seen, unique = set(), []
    for s in fresh:
        key = normalise_for_dedupe(s["title"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)
    unique.sort(key=lambda s: s["published"], reverse=True)
    unique = unique[:MAX_ITEMS]

    output = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "sources": health,
        "stories": unique,
    }
    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")

    counts = {k: sum(1 for s in unique if s["severity"] == k) for k in ("fatal", "major", "other")}
    print(f"Saved {len(unique)} stories  (fatal {counts['fatal']}, major {counts['major']}, other {counts['other']})")


if __name__ == "__main__":
    main()
