"""每天抓取各保險公司官網表格，偵測更新，輸出 docs/forms.json，並發 Telegram 通知。"""
import datetime, hashlib, html, json, os, re, sys, time
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "docs", "forms.json")
SOURCES_FILE = os.path.join(ROOT, "scraper", "sources.json")
TZ = datetime.timezone(datetime.timedelta(hours=8))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
}
GENERIC = {"download", "下載", "pdf", "download form", "download form (pdf)", "下載表格", "click here"}


def now():
    return datetime.datetime.now(TZ)


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def filename_of(url):
    return unquote(os.path.basename(urlparse(url).path))


def pretty_filename(url):
    name = filename_of(url)
    name = re.sub(r"(\.pdf)+(\.coredownload\.inline\.pdf)?$", "", name, flags=re.I)
    return clean(re.sub(r"[-_]+", " ", name))


def title_for(a, url):
    text = clean(a.get_text(" "))
    if len(text) >= 4 and text.lower() not in GENERIC:
        return text
    for attr in ("title", "aria-label", "data-title"):
        if clean(a.get(attr)) and clean(a.get(attr)).lower() not in GENERIC:
            return clean(a.get(attr))
    parent = a.parent
    for _ in range(3):
        if parent is None:
            break
        ptext = clean(parent.get_text(" ").replace(text, ""))
        if 4 <= len(ptext) <= 120:
            return ptext
        parent = parent.parent
    return pretty_filename(url)


def category_for(a, url, source):
    low = url.lower()
    for key, name in source.get("path_categories", {}).items():
        if key.lower() in low:
            return name
    heading = a.find_previous(["h2", "h3", "h4", "h5"])
    if heading and 2 <= len(clean(heading.get_text())) <= 60:
        return clean(heading.get_text())
    return "其他"


def extract_links(page_html, page_url, source):
    soup = BeautifulSoup(page_html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"].strip())
        if ".pdf" not in url.lower():
            continue
        if any(x.lower() in url.lower() for x in source.get("exclude", [])):
            continue
        url = url.split("#")[0]
        if url in found:
            continue
        found[url] = {"title": title_for(a, url), "category": category_for(a, url, source)}
    return found


def sha256_of(session, url):
    r = session.get(url, headers=HEADERS, timeout=60, stream=True)
    r.raise_for_status()
    h, size = hashlib.sha256(), 0
    for chunk in r.iter_content(65536):
        h.update(chunk)
        size += len(chunk)
    if size < 500:
        raise ValueError("檔案太小，可能不是 PDF")
    return h.hexdigest(), size


def form_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def load_previous():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def scrape(session, sources, previous):
    today = now().strftime("%Y-%m-%d")
    prev_forms = {f["id"]: f for f in (previous or {}).get("forms", [])}
    forms, statuses = [], []
    changes = {"new": [], "updated": [], "removed": []}

    for source in sources:
        company = source["company"]
        links, errors = {}, []
        for page in source["pages"]:
            try:
                r = session.get(page, headers=HEADERS, timeout=60)
                r.raise_for_status()
                links.update(extract_links(r.text, page, source))
            except Exception as e:
                errors.append(f"{page}：{e}")

        old = [f for f in prev_forms.values() if f["company"] == company]
        if not links:
            # 抓不到任何表格：保留舊資料，回報錯誤
            forms.extend(old)
            statuses.append({"company": company, "name": source["name"], "ok": False,
                             "count": len(old), "error": "; ".join(errors) or "頁面上找不到任何 PDF 連結（官網可能改版）"})
            continue

        seen = set()
        for url, meta in links.items():
            fid = form_id(url)
            seen.add(fid)
            prev = prev_forms.get(fid)
            entry = dict(prev) if prev else {"id": fid, "first_seen": today, "last_changed": today}
            entry.update({"company": company, "company_name": source["name"], "url": url,
                          "filename": filename_of(url), "title": meta["title"],
                          "category": meta["category"]})
            try:
                digest, size = sha256_of(session, url)
                entry.update({"sha256": digest, "size": size, "last_checked": today})
                entry.pop("error", None)
                if not prev:
                    changes["new"].append(entry)
                elif prev.get("sha256") and prev["sha256"] != digest:
                    entry["last_changed"] = today
                    changes["updated"].append(entry)
            except Exception as e:
                entry["error"] = str(e)[:200]
                if not prev:
                    entry.setdefault("sha256", None)
            forms.append(entry)
            time.sleep(0.5)  # 放慢速度，避免對官網造成負擔

        changes["removed"].extend(f for f in old if f["id"] not in seen)
        statuses.append({"company": company, "name": source["name"], "ok": not errors,
                         "count": len(seen), "error": "; ".join(errors) or None})
    return forms, statuses, changes


def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("（未設定 Telegram，略過通知）")
        return
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": c, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=30)


def build_message(changes, statuses, first_run, total, site_url):
    e = html.escape
    lines = []
    if first_run:
        lines.append(f"📋 <b>表格系統已建立</b>，目前共收錄 {total} 份表格。")
    else:
        if changes["updated"]:
            lines.append(f"🔄 <b>有 {len(changes['updated'])} 份表格改版</b>（請改用新版）")
            lines += [f"• [{e(f['company_name'])}] <a href=\"{e(f['url'])}\">{e(f['title'])}</a>"
                      for f in changes["updated"]]
            lines.append("")
        if changes["new"]:
            lines.append(f"🆕 <b>新增 {len(changes['new'])} 份表格</b>")
            lines += [f"• [{e(f['company_name'])}] <a href=\"{e(f['url'])}\">{e(f['title'])}</a>"
                      for f in changes["new"]]
            lines.append("")
        if changes["removed"]:
            lines.append(f"🗑 <b>官網已下架 {len(changes['removed'])} 份</b>")
            lines += [f"• [{e(f['company_name'])}] {e(f['title'])}" for f in changes["removed"]]
            lines.append("")
    bad = [s for s in statuses if not s["ok"]]
    if bad:
        lines.append("⚠️ <b>以下公司這次抓取失敗，資料可能不是最新：</b>")
        lines += [f"• {e(s['name'])}：{e(str(s['error'])[:200])}" for s in bad]
    if lines and site_url:
        lines.append(f"\n👉 {e(site_url)}")
    return "\n".join(lines).strip()


def main():
    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources = json.load(f)
    previous = load_previous()
    first_run = previous is None
    session = requests.Session()
    forms, statuses, changes = scrape(session, sources, previous)
    forms.sort(key=lambda f: (f["company"], f["category"], f["title"]))

    data = {
        "generated_at": now().strftime("%Y-%m-%d %H:%M"),
        "baseline_date": (previous or {}).get("baseline_date") or now().strftime("%Y-%m-%d"),
        "sources": statuses,
        "forms": forms,
    }
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    print(json.dumps(statuses, ensure_ascii=False, indent=1))
    print(f"新增 {len(changes['new'])}、改版 {len(changes['updated'])}、下架 {len(changes['removed'])}")
    msg = build_message(changes, statuses, first_run, len(forms), os.environ.get("SITE_URL", ""))
    if msg:
        send_telegram(msg)
    if all(not s["ok"] for s in statuses):
        sys.exit("所有來源都抓取失敗")


if __name__ == "__main__":
    main()
