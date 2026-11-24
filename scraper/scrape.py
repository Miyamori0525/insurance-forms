"""每天抓取各保險公司官網表格，偵測更新，輸出 docs/forms.json，並發 Telegram 通知。

sources.json 每家公司的設定：
  pages            要抓的頁面（可多個）
  browser          不填 = 直接下載網頁；"headless" = 用 Playwright 背景瀏覽器；
                   "headed" = 用有視窗的瀏覽器（官網會擋背景瀏覽器時用，GitHub 上配合 xvfb）
  click            瀏覽器模式下，要逐一點擊的分頁按鈕（CSS 選擇器），每點一次就收集一次連結
  include / exclude  網址或名稱包含這些字才收錄／就不收錄（不分大小寫）
  path_categories  網址包含某段字 → 指定分類
  category_map     官網分類名稱 → 統一分類名稱
"""
import datetime, hashlib, html, json, os, re, sys, time
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "docs", "forms.json")
SOURCES_FILE = os.path.join(ROOT, "scraper", "sources.json")
OVERRIDES_FILE = os.path.join(ROOT, "scraper", "title_overrides.json")
TZ = datetime.timezone(datetime.timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8"}

# 這些字樣不算表格名稱（例如「下載表格」「Download」）
GENERIC_RE = re.compile(
    r"^(?:下載表格|下載|按此下載|按此|表格|檔案|download form|download|click here|pdf|"
    r"opens in a new tab|在新視窗開啟|於新視窗開啟|網上提交索償|網上提交|上載表格|"
    r"中文|英文|繁|简|eng|english|chi|[\s()（）\[\]【】｜|/:：.\-–—]|\d+(?:\.\d+)?\s*[km]b)+$", re.I)
NOISE_RE = re.compile(r"\s*(opens in a new tab|在新視窗開啟|（?\(?\s*pdf\s*\)?）?|\(\s*\d+(\.\d+)?\s*[KM]B\s*\))\s*$", re.I)
LANG_NOTE = re.compile(r"\s*[–\-—]\s*(繁體中文\s*[/／]\s*英文|英文\s*[/／]\s*繁體中文|中英對照)\s*$")
FORM_WORD = re.compile(r"表|書|申請|聲明|授權|指示|問卷|報告|資料|form|claim", re.I)
HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"]


def now():
    return datetime.datetime.now(TZ)


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def is_generic(text):
    t = clean(text)
    return not t or bool(GENERIC_RE.match(t))


def filename_of(url):
    return unquote(os.path.basename(urlparse(url).path))


def pretty_filename(url):
    name = filename_of(url)
    name = re.sub(r"(\.pdf)+(\.coredownload\.inline\.pdf)?$", "", name, flags=re.I)
    return clean(re.sub(r"[-_]+", " ", name))


def tidy_title(t):
    t = clean(re.sub(r"\s*\(?PDF\)?\s*[\d.]+\s*[KM]B\)?", " ", t or "", flags=re.I))  # 「PDF 1.2 MB」
    for _ in range(2):
        t = NOISE_RE.sub("", t)
    t = re.sub(r"^\d{1,2}\.\s*", "", t)  # 去掉開頭編號「3. 」
    return clean(LANG_NOTE.sub("", t))


def is_pdf_link(url):
    return ".pdf" in url.lower()


# ---------- 名稱 ----------

def own_container(a, soup_links_of):
    """往上找，回傳「只包含這一份表格」的最外層容器。"""
    url = a["_abs"]
    node, best = a, None
    for _ in range(8):
        node = node.parent
        if node is None or node.name in ("body", "html", "[document]"):
            break
        others = {x["_abs"] for x in soup_links_of(node)} - {url}
        if others:
            break
        best = node
    return best


def strings_before(container, a):
    out = []
    for s in container.descendants:
        if s is a:
            break
        if isinstance(s, NavigableString) and s.parent is not None and s.parent.name not in ("script", "style"):
            t = clean(str(s))
            if t:
                out.append(t)
    return out


def raw_title(a, url, links_of, use_previous=False):
    text = clean(a.get_text(" "))
    if len(text) >= 4 and not is_generic(text):
        return text, "link"
    for attr in ("title", "aria-label", "data-title"):
        v = clean(a.get(attr))
        if (len(v) >= 4 and not is_generic(v) and not v.lower().endswith(".pdf")
                and not re.fullmatch(r"[A-Z0-9_\-]+", v)):  # 排除「PWS_HK_EN_G566_126」這類代碼
            return v, "attr"

    box = own_container(a, links_of)
    if box is not None:
        # 表格列：取連結所在欄位前面的欄位
        td = a.find_parent(["td", "th"])
        tr = a.find_parent("tr")
        if td is not None and tr is not None and (box is tr or tr in box.parents or box in tr.parents or box is td):
            def cell_text(c):  # 只取欄位本身的文字，不含欄位裡其他連結的字
                return clean(" ".join(s for s in c.find_all(string=True) if s.find_parent("a") is None))
            cells = [cell_text(c) for c in tr.find_all(["td", "th"], recursive=False)]
            idx = [i for i, c in enumerate(tr.find_all(["td", "th"], recursive=False)) if c is td]
            before = cells[:idx[0]] if idx else cells
            cand = [c for c in before if c and not is_generic(c)]
            pick = [c for c in cand if FORM_WORD.search(c)]
            if pick or cand:
                return (pick or cand)[-1], "table"
        # 標題字／粗體
        for h in box.find_all(HEADING_TAGS):
            t = clean(h.get_text(" "))
            if 4 <= len(t) <= 150 and not is_generic(t):
                return t, "heading"
        # 連結前面的第一段文字
        for t in strings_before(box, a):
            if 4 <= len(t) <= 150 and not is_generic(t):
                return t, "text"
        t = clean(box.get_text(" ").replace(text, ""))
        if 4 <= len(t) <= 150 and not is_generic(t):
            return t, "box"
    # 最後手段（需在 sources.json 開啟 title_from_previous）：連結前面最近的一段文字（例如上一段「3. 提交住院保障理賠」）
    for i, s in enumerate(a.find_all_previous(string=True) if use_previous else []):
        if i > 25:
            break
        if s.parent is None or s.parent.name in ("script", "style"):
            continue
        t = clean(str(s))
        if 4 <= len(t) <= 150 and not is_generic(t):
            return t, "previous"
    return pretty_filename(url), "filename"


# ---------- 分類 ----------

def category_for(a, url, source, tab_label, soup):
    low = url.lower()
    cmap = source.get("category_map", {})
    for key, name in source.get("path_categories", {}).items():
        if key.lower() in low:
            return name
    name = None
    anc_cls = source.get("category_ancestor_class")
    anc = a.find_parent(class_=anc_cls) if anc_cls else None
    if anc is not None:
        name = next((clean(s) for s in anc.find_all(string=True) if clean(s)), None)
    elif source.get("category_heading_class"):
        h = a.find_previous(class_=source["category_heading_class"])
        name = clean(h.get_text(" ")) if h is not None else None
    elif tab_label:
        name = tab_label
    else:
        panel = a.find_parent(attrs={"role": "tabpanel"}) if source.get("tabpanel_category") else None
        if panel is not None and panel.get("aria-labelledby"):
            el = soup.find(id=panel["aria-labelledby"])
            if el is not None:
                name = clean(el.get_text(" "))
        if not name:
            heading = a.find_previous(["h2", "h3", "h4", "h5"])
            if heading and 2 <= len(clean(heading.get_text())) <= 60:
                name = clean(heading.get_text())
    if not name:
        return "其他"
    return cmap.get(name, name)


# ---------- 擷取連結 ----------

def extract_links(page_html, page_url, source, tab_label=None, dbg=None):
    soup = BeautifulSoup(page_html, "html.parser")
    # 移除只給螢幕閱讀器看的隱藏文字（例如「內容將於新視窗顯示」）
    for el in soup.find_all(class_=re.compile(r"visuallyhidden|visually-hidden|sr-only|screen-reader")):
        el.decompose()
    anchors = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        a["_abs"] = urljoin(page_url, href).split("#")[0]
        if is_pdf_link(a["_abs"]):
            anchors.append(a)
    pdf_set = set(anchors)

    def links_of(node):
        return [x for x in node.find_all("a", href=True) if x in pdf_set] if isinstance(node, Tag) else []

    include = [x.lower() for x in source.get("include", [])]
    exclude = [x.lower() for x in source.get("exclude", [])]
    found = {}
    for a in anchors:
        url = a["_abs"]
        if url in found:
            continue
        title, how = raw_title(a, url, links_of, source.get("title_from_previous", False))
        title = tidy_title(title)
        hay = (url + " " + title).lower()
        if (any(x in hay for x in exclude) or (include and not any(x in hay for x in include))
                or (source.get("require_form_word") and not FORM_WORD.search(title))):
            if dbg is not None:
                dbg["excluded"] = dbg.get("excluded", 0) + 1
            continue
        category = category_for(a, url, source, tab_label, soup)
        if source.get("include_categories") and category not in source["include_categories"]:
            if dbg is not None:
                dbg["excluded"] = dbg.get("excluded", 0) + 1
            continue
        category = source.get("category_map", {}).get(category, category)
        for rule in source.get("title_rules", []):  # 依表格名稱關鍵字調整分類
            if re.search(rule["match"], title) and ("from" not in rule or category in rule["from"]):
                category = rule["to"]
                break
        found[url] = {"title": title, "category": category, "how": how}
    return found


# ---------- 瀏覽器（Playwright） ----------

class Browser:
    def __init__(self):
        self.pw = self.browser = self.ctx = None
        self.mode = None
        self.helpers = {}

    def start(self, mode):
        if self.ctx and self.mode == mode:
            return
        self.close()
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(
            headless=(mode != "headed"),
            args=["--disable-blink-features=AutomationControlled"])
        self.ctx = self.browser.new_context(
            locale="zh-HK", viewport={"width": 1366, "height": 900},
            **({"user_agent": UA} if mode != "headed" else {}))
        self.mode = mode

    def pages(self, url, source):
        """開啟頁面，回傳 [(html, 分頁名稱)]；有設定 click 會逐一點擊分頁。"""
        self.start(source.get("browser"))
        pg = self.ctx.new_page()
        out = []
        try:
            resp = pg.goto(url, wait_until="domcontentloaded", timeout=90000)
            if resp is not None and resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            try:
                pg.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            pg.wait_for_timeout(source.get("wait_ms", 3000))
            sel = source.get("click")
            if not sel:
                out.append((pg.content(), None))
            else:
                loc = pg.locator(sel).locator("visible=true")
                n = loc.count()
                if n == 0:
                    out.append((pg.content(), None))
                for i in range(n):
                    el = loc.nth(i)
                    label = clean(el.inner_text(timeout=5000))
                    try:
                        # 用程式直接觸發點擊，避免被廣告彈窗擋住
                        el.evaluate("e => { e.scrollIntoView({block: 'center'}); e.click(); }")
                        pg.wait_for_timeout(source.get("click_wait_ms", 2500))
                    except Exception as e:
                        print(f"  點擊「{label}」失敗：{str(e)[:150]}")
                        continue
                    out.append((pg.content(), label))
        finally:
            pg.close()
        return out

    def get_bytes(self, url, source):
        url = requests.utils.requote_uri(url)  # 中文檔名要先編碼
        r = self.ctx.request.get(url, timeout=90000)
        if r.ok and r.body()[:1024].find(b"%PDF") >= 0:
            return r.body()
        # 被擋（例如 403）：改在官網頁面裡用瀏覽器本身去下載
        return self.fetch_in_page(url, source, first_error=f"HTTP {r.status}")

    def fetch_in_page(self, url, source, first_error=""):
        first = source["pages"][0]
        first = first["url"] if isinstance(first, dict) else first
        key = source["company"]
        if key not in self.helpers:
            self.start(source.get("browser"))
            pg = self.ctx.new_page()
            pg.goto(first, wait_until="domcontentloaded", timeout=90000)
            pg.wait_for_timeout(3000)
            self.helpers[key] = pg
        pg = self.helpers[key]
        b64 = pg.evaluate("""async (u) => {
            const r = await fetch(u, {credentials: 'include'});
            if (!r.ok) return 'ERR' + r.status;
            const buf = new Uint8Array(await r.arrayBuffer());
            let s = ''; for (let i = 0; i < buf.length; i += 32768) s += String.fromCharCode.apply(null, buf.subarray(i, i + 32768));
            return btoa(s);
        }""", url)
        if b64.startswith("ERR"):
            raise RuntimeError(f"{first_error}；瀏覽器下載也失敗 HTTP {b64[3:]}")
        import base64
        return base64.b64decode(b64)

    def close(self):
        for pg in getattr(self, "helpers", {}).values():
            try:
                pg.close()
            except Exception:
                pass
        self.helpers = {}
        for x in (self.ctx, self.browser):
            try:
                x and x.close()
            except Exception:
                pass
        if self.pw:
            self.pw.stop()
        self.pw = self.browser = self.ctx = None


BROWSER = Browser()


def sha256_of(session, url, source):
    if source.get("browser"):
        data = BROWSER.get_bytes(url, source)
        if len(data) < 500 or b"%PDF" not in data[:1024]:
            raise ValueError("下載內容不是 PDF（可能被官網擋下）")
        return hashlib.sha256(data).hexdigest(), len(data)
    r = session.get(url, headers=HEADERS, timeout=60, stream=True)
    r.raise_for_status()
    h, size, head = hashlib.sha256(), 0, b""
    for chunk in r.iter_content(65536):
        if not head:
            head = chunk[:1024]
        h.update(chunk)
        size += len(chunk)
    if size < 500 or b"%PDF" not in head:
        raise ValueError("下載內容不是 PDF（可能被官網擋下）")
    return h.hexdigest(), size


def form_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def load_previous():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def collect(session, source):
    links, errors = {}, []
    dbg = {"method": source.get("browser") or "requests", "pages": []}
    for page in source["pages"]:
        page_cat = None
        if isinstance(page, dict):  # {"url": ..., "category": ...}：整頁指定分類
            page, page_cat = page["url"], page.get("category")
        info = {"url": page}
        try:
            if source.get("browser"):
                chunks = BROWSER.pages(page, source)
            else:
                r = session.get(page, headers=HEADERS, timeout=60)
                r.raise_for_status()
                chunks = [(r.text, None)]
            chunks = [(h, page_cat or label) for h, label in chunks]
            before = len(links)
            tabs = []
            for page_html, label in chunks:
                for url, meta in extract_links(page_html, page, source, label, dbg).items():
                    links.setdefault(url, meta)
                if label:
                    tabs.append(label)
            info["found"] = len(links) - before
            if tabs:
                info["tabs"] = tabs
        except Exception as e:
            info["error"] = str(e)[:300]
            errors.append(f"{page}：{str(e)[:200]}")
        dbg["pages"].append(info)
    hows = {}
    for m in links.values():
        hows[m["how"]] = hows.get(m["how"], 0) + 1
    dbg["title_source"] = hows
    return links, errors, dbg


def scrape(session, sources, previous, only=None):
    today = now().strftime("%Y-%m-%d")
    prev_forms = {f["id"]: f for f in (previous or {}).get("forms", [])}
    forms, statuses = [], []
    changes = {"new": [], "updated": [], "removed": []}

    for source in sources:
        company = source["company"]
        old = [f for f in prev_forms.values() if f["company"] == company]
        if only and company not in only:
            forms.extend(old)
            prev_status = next((s for s in (previous or {}).get("sources", []) if s["company"] == company), None)
            statuses.append(prev_status or {"company": company, "name": source["name"], "ok": False,
                                            "count": len(old), "error": "本次未執行"})
            continue
        print(f"== {source['name']}")
        try:
            links, errors, dbg = collect(session, source)
        except Exception as e:
            links, errors, dbg = {}, [str(e)[:300]], {"crash": str(e)[:300]}

        if not links:
            forms.extend(old)
            statuses.append({"company": company, "name": source["name"], "ok": False, "count": len(old),
                             "error": "; ".join(errors) or "頁面上找不到任何 PDF 連結（官網可能改版）",
                             "debug": dbg})
            continue

        seen, failed = set(), 0
        for url, meta in links.items():
            fid = form_id(url)
            seen.add(fid)
            prev = prev_forms.get(fid)
            entry = dict(prev) if prev else {"id": fid, "first_seen": today, "last_changed": today}
            entry.update({"company": company, "company_name": source["name"], "url": url,
                          "filename": filename_of(url), "title": meta["title"],
                          "category": meta["category"]})
            try:
                digest, size = sha256_of(session, url, source)
                entry.update({"sha256": digest, "size": size, "last_checked": today})
                entry.pop("error", None)
                if not prev:
                    changes["new"].append(entry)
                elif prev.get("sha256") and prev["sha256"] != digest:
                    entry["last_changed"] = today
                    changes["updated"].append(entry)
            except Exception as e:
                failed += 1
                entry["error"] = str(e)[:200]
                if not prev:
                    entry.setdefault("sha256", None)
            forms.append(entry)
            time.sleep(0.4)  # 放慢速度，避免對官網造成負擔

        dbg["download_failed"] = failed
        changes["removed"].extend(f for f in old if f["id"] not in seen)
        if failed and failed > len(seen) / 2:
            errors.append(f"{failed}/{len(seen)} 份 PDF 下載失敗")
        statuses.append({"company": company, "name": source["name"], "ok": not errors,
                         "count": len(seen), "error": "; ".join(errors) or None, "debug": dbg})
        print(f"   {len(seen)} 份，下載失敗 {failed}，{dbg.get('title_source')}")
    BROWSER.close()
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
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": c, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=30)
        if not r.ok:
            print("Telegram 傳送失敗：", r.status_code, r.text[:200])


def build_message(changes, statuses, first_run, total, site_url, new_companies=()):
    e = html.escape
    lines = []
    if first_run:
        lines.append(f"📋 <b>表格系統已建立</b>，目前共收錄 {total} 份表格。")
    else:
        if new_companies:
            lines.append(f"🏢 <b>新加入公司</b>：{e('、'.join(new_companies))}")
            lines.append("")
        new = [f for f in changes["new"] if f["company_name"] not in new_companies]
        if changes["updated"]:
            lines.append(f"🔄 <b>有 {len(changes['updated'])} 份表格改版</b>（請改用新版）")
            lines += [f"• [{e(f['company_name'])}] <a href=\"{e(f['url'])}\">{e(f['title'])}</a>"
                      for f in changes["updated"]]
            lines.append("")
        if new:
            lines.append(f"🆕 <b>新增 {len(new)} 份表格</b>")
            lines += [f"• [{e(f['company_name'])}] <a href=\"{e(f['url'])}\">{e(f['title'])}</a>"
                      for f in new]
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
    overrides = {}
    if os.path.exists(OVERRIDES_FILE):
        with open(OVERRIDES_FILE, encoding="utf-8") as f:
            overrides = json.load(f)
    only = set(a for a in sys.argv[1:] if not a.startswith("-")) or None  # 例：python scrape.py FWD MANULIFE
    previous = load_previous()
    first_run = previous is None
    prev_companies = {f["company"] for f in (previous or {}).get("forms", [])}
    session = requests.Session()
    forms, statuses, changes = scrape(session, sources, previous, only)
    for f in forms:
        key = f.get("filename")
        if key in overrides:
            f["title"] = overrides[key]
        elif f.get("url") in overrides:
            f["title"] = overrides[f["url"]]
    forms.sort(key=lambda f: (f["company"], f["category"], f["title"]))

    data = {
        "generated_at": now().strftime("%Y-%m-%d %H:%M"),
        "baseline_date": (previous or {}).get("baseline_date") or now().strftime("%Y-%m-%d"),
        "company_baseline": dict((previous or {}).get("company_baseline", {})),
        "sources": statuses,
        "forms": forms,
    }
    today = now().strftime("%Y-%m-%d")
    for s in statuses:
        if s["company"] not in prev_companies and s["count"] and s["company"] not in data["company_baseline"]:
            data["company_baseline"][s["company"]] = today
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    print(json.dumps([{k: v for k, v in s.items() if k != "debug"} for s in statuses], ensure_ascii=False, indent=1))
    print(f"新增 {len(changes['new'])}、改版 {len(changes['updated'])}、下架 {len(changes['removed'])}")
    new_companies = [s["name"] for s in statuses
                     if s["company"] not in prev_companies and s["count"] and not first_run]
    msg = build_message(changes, statuses, first_run, len(forms), os.environ.get("SITE_URL", ""), new_companies)
    if msg and "--no-notify" not in sys.argv:
        send_telegram(msg)
    if all(not s["ok"] for s in statuses):
        sys.exit("所有來源都抓取失敗")


if __name__ == "__main__":
    main()
