import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


CATEGORY_URL = (
    "https://www.ruparupa.com/c/barang-wajib-untuk-setiap-ruangan/"
    "ruang-kerja/kursi-kerja.html"
)
OUTPUT_CSV_FILE = "ruparupa_products.csv"
MAX_SCROLLS = 3


def extract_number(text):
    if not text:
        return None

    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def scrape_ruparupa():
    rows = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            locale="id-ID",
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/129.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        response = page.goto(
            CATEGORY_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        if not response or response.status >= 400:
            status = response.status if response else "none"
            raise RuntimeError(f"Page could not be loaded. HTTP status: {status}")

        page.wait_for_timeout(5000)

        for index in range(MAX_SCROLLS):
            page.mouse.wheel(0, 2000)
            print(f"Scrolling page {index + 1}/{MAX_SCROLLS}")
            time.sleep(2)

        soup = BeautifulSoup(page.content(), "html.parser")
        cards = soup.select("a[href*='/p/']")
        browser.close()

    scraped_at = datetime.now(
        timezone(timedelta(hours=7))
    ).strftime("%Y-%m-%d %H:%M:%S")

    seen_urls = set()

    for card in cards:
        card_text = card.get_text(" ", strip=True)
        card_text = re.sub(r"[\u200b-\u200d\ufeff]", "", card_text)
        lower_card_text = card_text.lower()
        if "kursi" not in lower_card_text:
            continue

        discount_match = re.search(
            r"\b\d+(?:[.,]\d+)?\s*%",
            card_text,
        )
        has_promo_label = bool(
            re.search(
                r"\b(buy\s*1\s*get\s*1|beli\s*1\s*gratis\s*1)\b",
                lower_card_text,
                flags=re.IGNORECASE,
            )
        )
        has_discount_price = len(re.findall(r"Rp\s*[\d.]+", card_text)) >= 2

        if has_promo_label and not discount_match and not has_discount_price:
            continue

        href = card.get("href")
        if not href:
            continue

        product_url = urljoin("https://www.ruparupa.com", href)
        if product_url in seen_urls:
            continue
        seen_urls.add(product_url)

        name_match = re.match(
            r"^(?:\d+\s+Var\s+Warna\s+)?(.+?)\s+Rp\s*[\d.]",
            card_text,
        )
        name = name_match.group(1).strip() if name_match else "N/A"

        prices = re.findall(r"Rp\s*[\d.]+", card_text)
        if len(prices) >= 2:
            original_price = extract_number(prices[0])
            current_price = extract_number(prices[-1])
        elif prices:
            original_price = extract_number(prices[0])
            current_price = original_price
        else:
            original_price = None
            current_price = None

        discount = discount_match.group() if discount_match else "0%"

        rating_match = re.search(
            r"\b([1-5](?:[.,]\d)?)\b\s+(\d+(?:[.,]\d+)?)\s*\(ulasan\)",
            card_text,
        )
        rating = (
            rating_match.group(1).replace(",", ".")
            if rating_match
            else None
        )
        review_match = re.search(r"\b(\d+)\s*\(ulasan\)", card_text)
        review_count = int(review_match.group(1)) if review_match else None

        image_element = card.select_one("img")
        image_url = None
        if image_element:
            image_url = (
                image_element.get("src")
                or image_element.get("data-src")
                or image_element.get("data-original")
            )
            if image_url:
                image_url = urljoin("https://www.ruparupa.com", image_url)

        lower_text = card_text.lower()
        if "habis" in lower_text or "out of stock" in lower_text:
            stock_status = "Out of stock"
        elif "buy 1 get 1" in lower_text:
            stock_status = "Available - promotion"
        else:
            stock_status = "Available"

        rows.append({
            "scraped_at": scraped_at,
            "keyword": "kursi kerja",
            "name": name,
            "original_price": original_price,
            "current_price": current_price,
            "discount": discount,
            "rating": rating,
            "review_count": review_count,
            "stock_status": stock_status,
            "url": product_url,
            "image_url": image_url,
        })

    if not rows:
        raise RuntimeError(
            "No products found. The page structure or product URL may have changed."
        )

    return pd.DataFrame(rows)


def append_to_csv(dataframe):
    file_exists = os.path.isfile(OUTPUT_CSV_FILE)
    dataframe.to_csv(
        OUTPUT_CSV_FILE,
        mode="a",
        header=not file_exists,
        index=False,
        encoding="utf-8-sig",
    )
    print(f"Appended {len(dataframe)} rows to {OUTPUT_CSV_FILE}")


if __name__ == "__main__":
    products = scrape_ruparupa()
    append_to_csv(products)
    print(products.head())
