"""
Reitbuch watcher for Reitclub Steinsee.
Reads the public Terminsuche page (no login needed) and sends a Telegram message when
  - new appointments are posted, or
  - a full / waitlisted appointment gets a free spot again.
Standard library only, so nothing needs to be installed.
"""
import datetime as dt
import html
import json
import os
import pathlib
import re
import urllib.parse
import urllib.request

BASE = "https://rc-steinsee.reitbuch.com"
URL = f"{BASE}/event.php"

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
# Optional: only report classes whose name contains one of these (comma-separated), e.g. "Dressur 2,Springen"
CLASS_FILTER = [k.strip().lower() for k in os.environ.get("CLASS_FILTER", "").split(",") if k.strip()]
NOTIFY_FREED = os.environ.get("NOTIFY_FREED", "true").lower() != "false"
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "60") or 60)
DEBUG = os.environ.get("DEBUG", "").lower() == "true"
# Optional: only these weekdays, e.g. "Fr,Sa,So" (German or English abbreviations)
DAY_NAMES = {"mo": 0, "di": 1, "tu": 1, "mi": 2, "we": 2, "do": 3, "th": 3,
             "fr": 4, "sa": 5, "so": 6, "su": 6}
ONLY_DAYS = {DAY_NAMES[d.strip().lower()[:2]] for d in os.environ.get("WEEKDAYS", "").split(",")
             if d.strip().lower()[:2] in DAY_NAMES}

STATE_FILE = pathlib.Path("state/events.json")

STATUS = {
    "free": "free spots",
    "mind": "free spots (minimum not reached yet)",
    "wait": "waitlist",
    "full": "full",
    "resv": "reserved",
}
OPEN = {"free", "mind"}
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

ROW_RE = re.compile(
    r'id="hit1_(\d+)">(.*?)</div>\s*<div[^>]*id="hit2_\1">\s*'
    r'<i class="[^"]*statcol_(\w+)[^"]*"></i>(.*?)</div>',
    re.S,
)


def fetch(data: dict | None = None) -> str:
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(URL, data=body, headers={"User-Agent": "Mozilla/5.0 (slot watcher)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(page: str) -> dict:
    events = {}
    for eid, when, status, title in ROW_RE.findall(page):
        when = " ".join(html.unescape(when).split())
        title = " ".join(html.unescape(re.sub(r"<[^>]+>", "", title)).replace("\u21f5", "").split())
        events[eid] = {"when": when, "status": status, "title": title}
    return events


def load_events() -> dict:
    today = dt.date.today()
    # Try a wider date range first; fall back to the default page if the site ignores it.
    try:
        events = parse(fetch({
            "search_from": today.isoformat(),
            "search_to": (today + dt.timedelta(days=DAYS_AHEAD)).isoformat(),
            "search_class": "", "search_wday": "", "search_teacher": "",
        }))
    except Exception:
        events = {}
    if not events:
        events = parse(fetch())
    return events


def sort_key(ev: dict):
    try:
        return dt.datetime.strptime(ev["when"], "%d.%m.%Y %H:%M")
    except ValueError:
        return dt.datetime.max


def fmt(ev: dict) -> str:
    d = sort_key(ev)
    day = f"{WEEKDAYS[d.weekday()]} " if d != dt.datetime.max else ""
    return f"{day}{ev['when']} - {ev['title']} [{STATUS.get(ev['status'], ev['status'])}]"


def wanted(ev: dict) -> bool:
    if ONLY_DAYS:
        d = sort_key(ev)
        if d == dt.datetime.max or d.weekday() not in ONLY_DAYS:
            return False
    return not CLASS_FILTER or any(k in ev["title"].lower() for k in CLASS_FILTER)


def notify(text: str) -> None:
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT, "text": text, "disable_web_page_preview": "true",
    }).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=20)


def send_list(header: str, items: list) -> None:
    lines = [fmt(e) for e in sorted(items, key=sort_key)]
    chunk, sent = [header], 0
    for line in lines:
        if sum(len(l) + 1 for l in chunk) + len(line) > 3500:
            notify("\n".join(chunk))
            sent += 1
            if sent >= 4:
                notify(f"...plus {len(lines) - lines.index(line)} more. See {URL}")
                return
            chunk = []
        chunk.append(line)
    chunk.append(f"\nBook: {URL}")
    notify("\n".join(chunk))


def main() -> None:
    current = load_events()
    if not current:
        raise RuntimeError("No appointments found on the page. The site layout may have changed.")

    if DEBUG:
        counts = {}
        for e in current.values():
            counts[e["status"]] = counts.get(e["status"], 0) + 1
        dates = sorted(current.values(), key=sort_key)
        notify(
            f"[Debug] {len(current)} appointments from {dates[0]['when']} to {dates[-1]['when']}.\n"
            f"Status counts: {counts}\n"
            f"Matching your filter: {sum(wanted(e) for e in current.values())}"
        )

    first_run = not STATE_FILE.exists()
    previous = {} if first_run else json.loads(STATE_FILE.read_text())

    new = [e for k, e in current.items() if k not in previous and wanted(e)]
    freed = [
        e for k, e in current.items()
        if k in previous and previous[k]["status"] not in OPEN and e["status"] in OPEN and wanted(e)
    ]

    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=0))

    if first_run:
        open_now = [e for e in current.values() if e["status"] in OPEN and wanted(e)]
        notify(f"Reitbuch watcher is live. Tracking {len(current)} appointments, "
               f"{len(open_now)} matching ones currently have free spots.")
        return
    if new:
        send_list(f"New appointments posted ({len(new)}):", new)
    if freed and NOTIFY_FREED:
        send_list("A spot opened up:", freed)


if __name__ == "__main__":
    main()
