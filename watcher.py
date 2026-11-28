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
MAX_RUN_SECONDS = 25 * 60  # 每輪跑 25 分鐘後安全存檔交棒

def get_exchange_rates():
    jpy_to_hkd = 0.050
    usd_to_jpy = 154.0
    try:
        res = requests.get("https://open.er-api.com/v6/latest/JPY", timeout=8)
        if res.status_code == 200:
            rates = res.json().get("rates", {})
            if rates.get("HKD"):
                jpy_to_hkd = float(rates["HKD"])
            if rates.get("USD"):
                usd_to_jpy = 1.0 / float(rates["USD"])
    except Exception:
        pass
    return jpy_to_hkd, usd_to_jpy

def format_price_hkd_first(yen_amount, jpy_to_hkd):
    if not yen_amount:
        return "價格未明"
    hkd_val = round(yen_amount * jpy_to_hkd, 1)
    return f"約 HK${hkd_val:,.1f}（¥{yen_amount:,}，匯率 {jpy_to_hkd:.4f}）"

def send_telegram_alert(text, image_bytes=None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ 缺少 TG_BOT_TOKEN 或 TG_CHAT_ID", flush=True)
        return
    
    if image_bytes:
        photo_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
        files = {"photo": ("item.jpg", image_bytes, "image/jpeg")}
        data = {
            "chat_id": TG_CHAT_ID,
            "caption": text,
            "parse_mode": "HTML"
        }
        try:
            res = requests.post(photo_url, data=data, files=files, timeout=10)
            if res.status_code == 200:
                print("✅ Telegram 圖文通知發送成功！", flush=True)
                return
            else:
                print(f"⚠️ 圖片上傳失敗（改用純文字重試）: {res.text}", flush=True)
        except Exception as e:
            print(f"⚠️ 圖片上傳異常: {e}", flush=True)

    msg_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(msg_url, json=payload, timeout=10)
        if res.status_code == 200:
            print("✅ Telegram 文字通知發送成功！", flush=True)
        else:
            print(f"❌ Telegram 文字發送失敗: {res.status_code} {res.text}", flush=True)
    except Exception as e:
        print(f"❌ Telegram 連線異常: {e}", flush=True)

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

def download_image_bytes(context, img_url):
    if not img_url or not img_url.startswith("http"):
        return None
    try:
        resp = context.request.get(img_url, timeout=6000)
        if resp.ok:
            return resp.body()
    except Exception as e:
        print(f"下載圖片失敗: {e}", flush=True)
    return None

def scan_mercari_and_send(page, context, keyword, known_list, known_set, is_first_time_kw, jpy_to_hkd, usd_to_jpy):
    encoded_kw = urllib.parse.quote(keyword)
    url = f"https://jp.mercari.com/search?keyword={encoded_kw}&sort=created_time&order=desc&status=on_sale"
    new_count = 0
    total_scanned = 0
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_selector("a[href*='/item/m']", timeout=10000)
            page.wait_for_timeout(400)
        except Exception:
            return 0, 0
        
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
            total_scanned += 1

            uid = f"Mercari_{item_id}"
            # 舊貨直接跳過，連標題同價錢都唔使讀
            if uid in known_set:
                continue

            known_set.add(uid)
            known_list.append(uid)
            new_count += 1

            if is_first_time_kw:
                continue

            # 發現新貨：即刻讀取呢一件嘅詳情並秒發 Telegram
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

            yen_price = None
            pm_yen = re.search(r"[¥￥]\s*([0-9,]+)|([0-9,]+)\s*円", combined_text)
            if pm_yen:
                val = pm_yen.group(1) or pm_yen.group(2)
                yen_price = int(val.replace(",", ""))
            else:
                pm_usd = re.search(r"US\$\s*([0-9,.]+)", combined_text)
                if pm_usd:
                    usd_val = float(pm_usd.group(1).replace(",", ""))
                    yen_price = int(round(usd_val * usd_to_jpy))

            cleaned_text = re.sub(r"US\$\s*[0-9,.]+\s*", "", raw_text)
            cleaned_text = re.sub(r"[¥￥]\s*[0-9,]+\s*", "", cleaned_text)
            lines = [x.strip() for x in cleaned_text.split("\n") if x.strip()]
            
            title_parts = []
            for line in lines:
                if re.search(r"^[0-9,]+\s*円$", line):
                    continue
                if "SOLD" in line.upper() or "売り切れ" in line:
                    continue
                title_parts.append(line)

            title = (" ".join(title_parts) if title_parts else "Mercari 新上架商品")[:80]
            print(f"⚡ 逐條秒發：[Mercari] {title} ({item_id})", flush=True)
            price_str = format_price_hkd_first(yen_price, jpy_to_hkd)
            img_bytes = download_image_bytes(context, img_url)

            msg = (
                f"🆕 <b>{title}</b>\n"
                f"平台：Mercari\n"
                f"價格：<b>{price_str}</b>\n"
                f"關鍵詞：{keyword}\n"
                f"ID：<code>{item_id}</code>（點擊複製）"
            )
            send_telegram_alert(msg, img_bytes)

    except Exception as e:
        print(f"❌ Mercari 搜尋 [{keyword}] 出錯: {e}", flush=True)
    return new_count, total_scanned

def scan_yahoo_and_send(page, context, keyword, known_list, known_set, is_first_time_kw, jpy_to_hkd):
    encoded_kw = urllib.parse.quote(keyword)
    url = f"https://auctions.yahoo.co.jp/search/search?p={encoded_kw}&va={encoded_kw}&exflg=1&b=1&n=20&s1=new&o1=d"
    new_count = 0
    total_scanned = 0
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_selector("li.Product", timeout=8000)
        except Exception:
            return 0, 0
        
        items = page.locator("li.Product").all()
        for item in items[:15]:
            title_el = item.locator("a.Product__titleLink").first
            if title_el.count() == 0:
                continue
            href = title_el.get_attribute("href") or ""
            m = re.search(r"/auction/([a-zA-Z0-9]+)", href)
            if not m:
                continue
            item_id = m.group(1)
            total_scanned += 1

            uid = f"Yahoo! 拍賣_{item_id}"
            # 舊貨直接跳過
            if uid in known_set:
                continue

            known_set.add(uid)
            known_list.append(uid)
            new_count += 1

            if is_first_time_kw:
                continue

            # 發現新貨：即刻讀取詳情並秒發 Telegram
            title = title_el.inner_text().strip()[:80]

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

            print(f"⚡ 逐條秒發：[Yahoo! 拍賣] {title} ({item_id})", flush=True)
            price_str = format_price_hkd_first(price, jpy_to_hkd)
            img_bytes = download_image_bytes(context, img_url)

            msg = (
                f"🆕 <b>{title}</b>\n"
                f"平台：Yahoo! 拍賣\n"
                f"價格：<b>{price_str}</b>\n"
                f"關鍵詞：{keyword}\n"
                f"ID：<code>{item_id}</code>（點擊複製）"
            )
            send_telegram_alert(msg, img_bytes)

    except Exception as e:
        print(f"❌ Yahoo 拍賣搜尋 [{keyword}] 出錯: {e}", flush=True)
    return new_count, total_scanned

def main():
    keywords = load_keywords()
    if not keywords:
        print("⚠️ keywords.txt 為空，無關鍵詞需要監控", flush=True)
        return

    jpy_to_hkd, usd_to_jpy = get_exchange_rates()

    seen_data = {}
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                seen_data = json.load(f)
        except Exception:
            seen_data = {}

    print("🚀 啟動逐條秒發搶貨模式（見到新 ID 即刻彈通知）...", flush=True)
    start_time = time.time()
    round_num = 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="ja-JP",
            timezone_id="Asia/Tokyo"
        )
        context.add_cookies([
            {"name": "mercari_currency", "value": "JPY", "domain": ".mercari.com", "path": "/"},
            {"name": "country_code", "value": "JP", "domain": ".mercari.com", "path": "/"}
        ])
        page = context.new_page()
        page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["font", "media"] else route.continue_())

        while time.time() - start_time < MAX_RUN_SECONDS:
            updated = False
            try:
                for kw in keywords:
                    is_first_time_kw = kw not in seen_data
                    if is_first_time_kw:
                        seen_data[kw] = []

                    known_list = seen_data[kw]
                    known_set = set(known_list)

                    mc_new, mc_total = scan_mercari_and_send(page, context, kw, known_list, known_set, is_first_time_kw, jpy_to_hkd, usd_to_jpy)
                    yh_new, yh_total = scan_yahoo_and_send(page, context, kw, known_list, known_set, is_first_time_kw, jpy_to_hkd)

                    total_new = mc_new + yh_new
                    if total_new > 0:
                        updated = True

                    if round_num % 10 == 1:
                        print(f"🔍 第 {round_num} 輪 [{kw}]：Mercari {mc_total} 件 | Yahoo! {yh_total} 件", flush=True)

                    if is_first_time_kw and total_new > 0:
                        print(f"📌 初始化關鍵詞 [{kw}]，已收錄 {total_new} 件現有基準", flush=True)
                        send_telegram_alert(f"📌 <b>關鍵詞上新監控已啟動！</b>\n關鍵詞：<code>{kw}</code>\n已建立現有基準（共 {total_new} 件），一有新貨即逐條秒發通知。")
                    elif is_first_time_kw and total_new == 0:
                        del seen_data[kw]

                    if kw in seen_data:
                        seen_data[kw] = known_list[-500:]

                if updated:
                    with open(SEEN_FILE, "w", encoding="utf-8") as f:
                        json.dump(seen_data, f, indent=2, ensure_ascii=False)

            except Exception as e:
                print(f"輪巡異常: {e}", flush=True)

            round_num += 1
            time.sleep(5)

        browser.close()

    print("⏱️ 本輪 25 分鐘巡查結束，儲存記錄並交棒下一輪。", flush=True)

if __name__ == "__main__":
    main()
