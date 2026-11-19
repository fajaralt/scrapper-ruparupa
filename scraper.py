import os
import re
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
MAX_SCROLLS = 10
STABLE_SCROLLS_TO_STOP = 2
MAX_PAGES = 100
SCROLL_DISTANCE = 2000
SCROLL_WAIT_MS = 2000
BASE_URL = "https://www.ruparupa.com"


def extract_number(text):
    if not text:
        return None

    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def product_cards(soup):
    cards = soup.select("a[href*='/p/']")
    if cards:
        return cards
    return soup.select(
        "[data-product-id] a[href], "
        "[data-testid*='product'] a[href], "
        "[class*='product-card'] a[href]"
    )


def product_count(soup):
    return len(
        {
            urljoin(BASE_URL, card.get("href"))
            for card in product_cards(soup)
            if card.get("href")
        }
    )


def next_page_url(soup, current_url):
    selectors = (
        "a[rel='next']",
        "a[aria-label*='Next']",
        "a[aria-label*='next']",
        "a[title*='Next']",
        "a[title*='next']",
    )
    for selector in selectors:
        next_link = soup.select_one(selector)
        if next_link and next_link.get("href"):
            return urljoin(current_url, next_link["href"])

    for link in soup.select("a[href]"):
        label = link.get_text(" ", strip=True).lower()
        if label in {"next", "berikutnya", "selanjutnya", "›", "»"}:
            return urljoin(current_url, link["href"])
    return None


def first_text(element, selectors):
    for selector in selectors:
        candidate = element.select_one(selector)
        if candidate:
            text = candidate.get_text(" ", strip=True)
            if text:
                return text
    return None


def price_texts(card):
    semantic_prices = []
    for element in card.select(
        "[data-testid*='price'], [class*='price'], [class*='Price']"
    ):
        text = element.get_text(" ", strip=True)
        if re.search(r"Rp\s*[\d.]+", text, flags=re.IGNORECASE):
            semantic_prices.append(text)

    prices = re.findall(
        r"Rp\s*[\d.]+", " ".join(semantic_prices), flags=re.IGNORECASE
    )
    if not prices:
        prices = re.findall(r"Rp\s*[\d.]+", card.get_text(" ", strip=True))
    return list(dict.fromkeys(prices))


def extract_product_name(card, card_text):
    name = first_text(
        card,
        (
            "[data-testid*='product-name']",
            "[data-testid*='name']",
            "[class*='product-name']",
            "[class*='productName']",
            "h2",
            "h3",
            "h4",
        ),
    )
    if name:
        return name

    name_match = re.search(
        r"^(?:\d+\s+Var\s+Warna\s+)?(.+?)\s+Rp\s*[\d.]",
        card_text,
        flags=re.IGNORECASE,
    )
    return name_match.group(1).strip() if name_match else "N/A"


def extract_prices(card):
    prices = price_texts(card)
    if len(prices) >= 2:
        original_price = extract_number(prices[0])
        current_price = extract_number(prices[-1])
    elif prices:
        original_price = extract_number(prices[0])
        current_price = original_price
    else:
        original_price = None
        current_price = None

    price_elements = card.select(
        "[data-testid*='price'], [class*='price'], [class*='Price']"
    )
    for element in price_elements:
        label = " ".join(element.get("class", []))
        label += f" {element.get('id', '')}"
        text = element.get_text(" ", strip=True)
        value = extract_number(
            re.search(r"Rp\s*[\d.]+", text, flags=re.IGNORECASE).group()
            if re.search(r"Rp\s*[\d.]+", text, flags=re.IGNORECASE)
            else None
        )
        if value is None:
            continue
        if re.search(r"old|original|before|normal|strike", label, re.I):
            original_price = value
        elif re.search(r"sale|current|selling|final|discount", label, re.I):
            current_price = value

    if (
        original_price is not None
        and current_price is not None
        and current_price > original_price
    ):
        original_price, current_price = current_price, original_price
    return original_price, current_price


def scrape_page(page):
    stable_scrolls = 0
    previous_count = product_count(
        BeautifulSoup(page.content(), "html.parser")
    )

    for index in range(MAX_SCROLLS):
        page.mouse.wheel(0, SCROLL_DISTANCE)
        print(f"Scrolling page {index + 1}/{MAX_SCROLLS}")
        page.wait_for_timeout(SCROLL_WAIT_MS)

        current_count = product_count(
            BeautifulSoup(page.content(), "html.parser")
        )
        if current_count <= previous_count:
            stable_scrolls += 1
            if stable_scrolls >= STABLE_SCROLLS_TO_STOP:
                break
        else:
            stable_scrolls = 0
            previous_count = current_count

    return BeautifulSoup(page.content(), "html.parser")


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

        page_url = CATEGORY_URL
        page_snapshots = []
        visited_pages = set()
        for page_number in range(MAX_PAGES):
            if page_url in visited_pages:
                break
            visited_pages.add(page_url)

            response = page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            if not response or response.status >= 400:
                status = response.status if response else "none"
                raise RuntimeError(
                    f"Page could not be loaded. HTTP status: {status}"
                )

            page.wait_for_timeout(5000)
            soup = scrape_page(page)
            page_snapshots.append(soup)
            page_url = next_page_url(soup, page.url)
            if not page_url:
                break
            print(f"Reading pagination page {page_number + 2}/{MAX_PAGES}")
        browser.close()

    scraped_at = datetime.now(
        timezone(timedelta(hours=7))
    ).strftime("%Y-%m-%d %H:%M:%S")

    seen_urls = set()

    for soup in page_snapshots:
        for card in product_cards(soup):
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
            has_discount_price = (
                len(re.findall(r"Rp\s*[\d.]+", card_text)) >= 2
            )

            if has_promo_label and not discount_match and not has_discount_price:
                continue

            href = card.get("href")
            if not href:
                continue

            product_url = urljoin(BASE_URL, href)
            if product_url in seen_urls:
                continue
            seen_urls.add(product_url)

            name = extract_product_name(card, card_text)
            original_price, current_price = extract_prices(card)

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
                    image_url = urljoin(BASE_URL, image_url)

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
