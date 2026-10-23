
import requests
import feedparser
import sys

print("Python:", sys.executable)

url = "https://rsshub.rssforever.com/leetcode/dailyquestion/solution/en"

try:
    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    print("HTTP status:", r.status_code)
    print("Content-Type:", r.headers.get("Content-Type"))
    print("Response preview:", repr(r.text[:500]))

    r.raise_for_status()
    feed = feedparser.parse(r.content)

    print("Entries:", len(feed.entries))
    print("Parser error:", getattr(feed, "bozo_exception", None))

    for entry in feed.entries[:3]:
        print("Title:", entry.get("title"))
        print("Link:", entry.get("link"))

except Exception as e:
    print(type(e).__name__, e)
