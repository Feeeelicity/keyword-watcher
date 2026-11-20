import os
import re
import json
import time
import urllib.parse
import requests
from playwright.sync_api import sync_playwright

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")
KEYWORDS_FILE = "keywords.txt"
SEEN_FILE = "seen_items.json"

def get_live_exchange_rate():
    try:
        res = requests.get("https://open.er-api.com/v6/latest/JPY", timeout=8)
        if res.status_code == 200:
            rate = res.json().get("rates", {}).get("HKD")
            if rate:
                return float(rate)
    except Exception:
        pass
    try:
        res = requests.get("https://api.frankfurter.app/latest?from=JPY&to=HKD", timeout=8)
        if res.status_code == 200:
            rate = res.json().get("rates", {}).get("HKD")
            if rate:
                return float(rate)
    except Exception:
        pass
    return 0.050

def format_price_hkd_first(yen_amount, rate):
    if not yen_amount:
        return "價格未明"
    hkd_val = round(yen_amount * rate, 1)
    return f"約 HK${hkd_val:,.1f}（¥{yen_amount:,}，匯率 {rate:.4f}）"

def send_telegram_alert(text, image_url=None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    
    if image_url and image_url.startswith("http"):
        photo_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": TG_CHAT_ID,
            "photo": image_url,
            "caption": text,
            "parse_mode": "HTML"
        }
        try:
            res = requests.post(photo_url, json=payload, timeout=12)
            if res.status_code == 200:
                return
            else:
                print(f"圖片發送失敗（改用文字重試）: {res.text}")
        except Exception as e:
            print(f"圖片發送異常: {e}")

    msg_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(msg_url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram 文字發送失敗: {e}")

def load_keywords():
    keywords = []
    if not os.path.exists(KEYWORDS_FILE):
        return keywords
    with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            kw = line.split("#")[0].strip()
            if kw:
                keywords.append(kw)
    return keywords

def scrape_mercari(page, keyword):
    encoded_kw = urllib.parse.quote(keyword)
    url = f"https://jp.mercari.com/search?keyword={encoded_kw}&sort=created_time&order=desc&status=on_sale"
    results = []
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3500)
        
        links = page.locator("a[href*='/item/m']").all()
        seen_in_page = set()
        for link in links[:15]:
            href = link.get_attribute("href") or ""
            m = re.search(r"(m\d+)", href)
            if not m:
                continue
            item_id = m.group(1)
            if item_id in seen_in_page:
                continue
            seen_in_page.add(item_id)

            img_url = None
            try:
                img_el = link.locator("img").first
                if img_el.count() > 0:
                    img_url = img_el.get_attribute("src") or img_el.get_attribute("data-src")
            except Exception:
                pass

            raw_text = link.inner_text().strip()
            aria_label = link.get_attribute("aria-label") or ""
            combined_text = f"{raw_text}\n{aria_label}"

            price = None
            pm = re.search(r"[¥￥]\s*([0-9,]+)|([0-9,]+)\s*円", combined_text)
            if pm:
                val = pm.group(1) or pm.group(2)
                price = int(val.replace(",", ""))

            lines = [x.strip() for x in raw_text.split("\n") if x.strip()]
            title_parts = []
            for line in lines:
                if re.search(r"^[¥￥]\s*[0-9,]+$|^[0-9,]+\s*円$", line):
                    continue
                if "SOLD" in line.upper() or "売り切れ" in line:
                    continue
                title_parts.append(line)

            title = " ".join(title_parts) if title_parts else "Mercari 新上架商品"
            results.append({
                "platform": "Mercari",
                "id": item_id,
                "title": title[:60],
                "price": price,
                "image": img_url
            })
    except Exception as e:
        print(f"Mercari 搜尋 [{keyword}] 出錯: {e}")
    return results

def scrape_yahoo_auctions(page, keyword):
    encoded_kw = urllib.parse.quote(keyword)
    url = f"https://auctions.yahoo.co.jp/search/search?p={encoded_kw}&va={encoded_kw}&exflg=1&b=1&n=20&s1=new&o1=d"
    results = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        
        items = page.locator("li.Product").all()
        for item in items[:15]:
            title_el = item.locator("a.Product__titleLink").first
            if title_el.count() == 0:
                continue
            href = title_el.get_attribute("href") or ""
            title = title_el.inner_text().strip()
            
            m = re.search(r"/auction/([a-zA-Z0-9]+)", href)
            if not m:
                continue
            item_id = m.group(1)

            img_url = None
            try:
                img_el = item.locator("img.Product__imageData").first
                if img_el.count() == 0:
                    img_el = item.locator("img").first
                if img_el.count() > 0:
                    img_url = img_el.get_attribute("src") or img_el.get_attribute("data-src")
            except Exception:
                pass

            price = None
            price_el = item.locator(".Product__priceValue").first
            if price_el.count() > 0:
                p_text = price_el.inner_text()
                pm = re.search(r"([0-9,]+)", p_text)
                if pm:
                    price = int(pm.group(1).replace(",", ""))

            results.append({
                "platform": "Yahoo! 拍賣",
                "id": item_id,
                "title": title[:60],
                "price": price,
                "image": img_url
            })
    except Exception as e:
        print(f"Yahoo 拍賣搜尋 [{keyword}] 出錯: {e}")
    return results

def run_watch():
    keywords = load_keywords()
    if not keywords:
        print("keywords.txt 為空，無關鍵詞需要監控")
        return

    rate = get_live_exchange_rate()

    seen_data = {}
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                seen_data = json.load(f)
        except Exception:
            seen_data = {}

    updated = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="ja-JP"
        )
        page = context.new_page()

        for kw in keywords:
            is_first_time_kw = kw not in seen_data
            if is_first_time_kw:
                seen_data[kw] = []

            known_ids = set(seen_data[kw])

            mc_items = scrape_mercari(page, kw)
            yh_items = scrape_yahoo_auctions(page, kw)
            all_found = mc_items + yh_items

            new_count = 0
            for item in all_found:
                uid = f"{item['platform']}_{item['id']}"
                if uid not in known_ids:
                    known_ids.add(uid)
                    updated = True
                    new_count += 1

                    if not is_first_time_kw:
                        price_str = format_price_hkd_first(item["price"], rate)
                        msg = (
                            f"🆕 <b>{item['title']}</b>\n"
                            f"平台：{item['platform']}\n"
                            f"價格：<b>{price_str}</b>\n"
                            f"關鍵詞：{kw}\n"
                            f"ID：<code>{item['id']}</code>（點擊複製）"
                        )
                        send_telegram_alert(msg, item.get("image"))

            if is_first_time_kw:
                print(f"📌 初始化關鍵詞 [{kw}]，已收錄 {new_count} 件現有商品基準")
                send_telegram_alert(f"📌 <b>關鍵詞上新監控已啟動！</b>\n關鍵詞：<code>{kw}</code>\n已建立現有商品基準（共 {new_count} 件），之後一有新上架即刻發圖通知。")

            seen_data[kw] = list(known_ids)[-300:]

        browser.close()

    if updated:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(seen_data, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    print("🚀 啟動 Mercari + 雅虎拍賣 極速圖文上新監控...")
    for i in range(400):
        try:
            run_watch()
        except Exception as e:
            print(f"輪巡異常: {e}")
        time.sleep(12)
