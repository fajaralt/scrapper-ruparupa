import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

try:
    from supabase import create_client
except ImportError:
    create_client = None


OUTPUT_CSV_FILE = "ruparupa_products.csv"
MAX_SCROLLS = 5
STABLE_SCROLLS_TO_STOP = 2
MAX_PAGES = 100
SCROLL_DISTANCE = 2000
SCROLL_WAIT_MS = 2000
MAX_CATEGORIES_PER_RUN = 10
BASE_URL = "https://www.ruparupa.com"
CATEGORY_INDEX_URL = f"{BASE_URL}/c/"
CSV_COLUMNS = [
    "product_id",
    "name",
    "brand",
    "category",
    "subcategory",
    "original_price",
    "current_price",
    "discount",
    "promotion",
    "stock_status",
    "rating",
    "review_count",
    "scraped_at",
]


def extract_number(text):
    if not text:
        return None

    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def product_cards(soup):
    cards = soup.select(".product-card a[href*='/p/']")
    if cards:
        return cards
    return soup.select(
        "[data-product-id] a[href*='/p/'], "
        "[data-testid*='product'] a[href*='/p/']"
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


def discover_category_urls(soup):
    category_urls = set()
    for link in soup.select("a[href]"):
        href = link.get("href", "")
        absolute_url = urljoin(BASE_URL, href)
        if absolute_url.startswith(f"{BASE_URL}/c/") and absolute_url != CATEGORY_INDEX_URL:
            category_urls.add(absolute_url)
    return sorted(category_urls)


def normalize_product_url(url):
    if not isinstance(url, str) or not url:
        return None
    absolute_url = urljoin(BASE_URL, url)
    parts = urlsplit(absolute_url)
    if "/p/" not in parts.path:
        return absolute_url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def extract_product_id(product_url):
    if not isinstance(product_url, str):
        return None
    path = urlsplit(urljoin(BASE_URL, product_url)).path
    filename = path.rsplit("/", 1)[-1]
    if filename.endswith(".html"):
        filename = filename[:-5]
    return filename or None


def extract_sku(product_url):
    if not isinstance(product_url, str):
        return None
    return parse_qs(urlsplit(product_url).query).get("itm_term", [None])[0]


def category_fields(source_category):
    if not isinstance(source_category, str):
        return None, None
    segments = [
        segment
        for segment in urlsplit(source_category).path.split("/")
        if segment and segment != "c"
    ]
    if not segments:
        return None, None
    category = segments[0].replace("-", " ").strip().title()
    subcategory = (
        segments[-1].replace("-", " ").strip().title()
        if len(segments) > 1
        else category
    )
    return category, subcategory


def extract_brand(name):
    if not isinstance(name, str) or name == "N/A":
        return None
    words = name.split()
    return words[0].strip() if words else None


def clean_product_name(name):
    if not isinstance(name, str) or not name:
        return "N/A"
    name = re.sub(r"[\u200b-\u200d\ufeff]", "", name)
    name = name.strip()
    name = re.sub(
        r"^(?:\d+\s+(?:Tipe|Ukuran|Var\s+Warna)\s+)",
        "",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(r"\bHanya\s*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "N/A"


def clean_image_url(card):
    for image_element in card.select("img[src], img[data-src], img[data-original]"):
        image_url = (
            image_element.get("src")
            or image_element.get("data-src")
            or image_element.get("data-original")
        )
        if not image_url:
            continue
        absolute_url = urljoin(BASE_URL, image_url)
        lower_url = absolute_url.lower()
        if lower_url.endswith(".svg") or "/icon/" in lower_url:
            continue
        if "ruparupa" not in lower_url:
            continue
        if "/products/" not in lower_url:
            continue
        return absolute_url
    return None


def normalize_discount(discount):
    if not discount:
        return "0%"
    match = re.search(r"\d+(?:[.,]\d+)?", str(discount))
    if not match:
        return "0%"
    return f"{match.group().replace(',', '.')}%"


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
        return clean_product_name(name)

    name_match = re.search(
        r"^(?:\d+\s+Var\s+Warna\s+)?(.+?)\s+Rp\s*[\d.]",
        card_text,
        flags=re.IGNORECASE,
    )
    if name_match:
        return clean_product_name(name_match.group(1))
    return "N/A"


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


def load_page(page, page_url):
    response = page.goto(
        page_url,
        wait_until="domcontentloaded",
        timeout=60000,
    )
    if not response or response.status >= 400:
        status = response.status if response else "none"
        raise RuntimeError(f"Page could not be loaded. HTTP status: {status}")
    page.wait_for_timeout(3000)


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

        configured_categories = os.getenv("RUPARUPA_CATEGORY_URLS", "")
        if configured_categories:
            category_urls = [
                url.strip()
                for url in configured_categories.split(",")
                if url.strip()
            ]
            if not category_urls:
                raise RuntimeError(
                    "RUPARUPA_CATEGORY_URLS is set but contains no category URL."
                )
        else:
            load_page(page, CATEGORY_INDEX_URL)
            category_urls = discover_category_urls(
                BeautifulSoup(page.content(), "html.parser")
            )
            if not category_urls:
                raise RuntimeError(
                    "No category URLs found on "
                    f"{CATEGORY_INDEX_URL}. Set RUPARUPA_CATEGORY_URLS "
                    "with one or more explicit category URLs."
                )

        batch_size = MAX_CATEGORIES_PER_RUN
        batch_size_override = os.getenv("RUPARUPA_CATEGORY_BATCH_SIZE", "")
        if batch_size_override:
            try:
                batch_size = min(
                    int(batch_size_override),
                    MAX_CATEGORIES_PER_RUN,
                )
            except ValueError as error:
                raise RuntimeError(
                    "RUPARUPA_CATEGORY_BATCH_SIZE must be an integer."
                ) from error
            if batch_size < 1:
                raise RuntimeError(
                    "RUPARUPA_CATEGORY_BATCH_SIZE must be at least 1."
                )

        batch_index_text = os.getenv(
            "RUPARUPA_CATEGORY_BATCH_INDEX",
            "0",
        )
        try:
            batch_index = int(batch_index_text)
        except ValueError as error:
            raise RuntimeError(
                "RUPARUPA_CATEGORY_BATCH_INDEX must be an integer."
            ) from error
        if batch_index < 0:
            raise RuntimeError(
                "RUPARUPA_CATEGORY_BATCH_INDEX cannot be negative."
            )

        total_categories = len(category_urls)
        batch_start = batch_index * batch_size
        category_urls = category_urls[batch_start:batch_start + batch_size]
        if not category_urls:
            raise RuntimeError(
                f"No categories found for batch index {batch_index} "
                f"with batch size {batch_size}. "
                "Choose another batch index."
            )

        print(
            f"Running category batch {batch_index + 1}; "
            f"processing {len(category_urls)} of {total_categories} "
            "available URLs."
        )

        page_snapshots = []
        for category_number, category_url in enumerate(category_urls, start=1):
            page_url = category_url
            visited_pages = set()
            for page_number in range(MAX_PAGES):
                if page_url in visited_pages:
                    break
                visited_pages.add(page_url)

                load_page(page, page_url)
                soup = scrape_page(page)
                page_snapshots.append((soup, category_url))
                page_url = next_page_url(soup, page.url)
                if not page_url:
                    break
                print(
                    f"Reading category {category_number}/{len(category_urls)}, "
                    f"page {page_number + 2}/{MAX_PAGES}"
                )
        browser.close()

    scraped_at = datetime.now(
        timezone(timedelta(hours=7))
    ).strftime("%Y-%m-%d %H:%M:%S")

    seen_urls = set()

    for soup, source_category in page_snapshots:
        for card in product_cards(soup):
            card_text = card.get_text(" ", strip=True)
            card_text = re.sub(r"[\u200b-\u200d\ufeff]", "", card_text)
            lower_card_text = card_text.lower()

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

            href = card.get("href")
            if not href:
                continue

            product_id = extract_sku(urljoin(BASE_URL, href))
            product_url = normalize_product_url(href)
            if not product_url:
                continue
            if not product_id:
                continue
            if product_id in seen_urls:
                continue
            seen_urls.add(product_id)

            name = extract_product_name(card, card_text)
            category, subcategory = category_fields(source_category)
            brand = extract_brand(name)
            original_price, current_price = extract_prices(card)

            discount = normalize_discount(
                discount_match.group() if discount_match else "0%"
            )

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

            lower_text = card_text.lower()
            if "habis" in lower_text or "out of stock" in lower_text:
                stock_status = "Out of stock"
            elif "buy 1 get 1" in lower_text:
                stock_status = "Available - promotion"
            else:
                stock_status = "Available"

            rows.append({
                "product_id": product_id,
                "name": name,
                "brand": brand,
                "category": category,
                "subcategory": subcategory,
                "original_price": original_price,
                "current_price": current_price,
                "discount": discount,
                "promotion": "Buy 1 Get 1" if has_promo_label else None,
                "stock_status": stock_status,
                "rating": rating,
                "review_count": review_count,
                "scraped_at": scraped_at,
            })

    if not rows:
        raise RuntimeError(
            "No products found. The page structure or product URL may have changed."
        )

    return pd.DataFrame(rows).reindex(columns=CSV_COLUMNS)


def append_to_csv(dataframe):
    dataframe = dataframe.copy()
    dataframe["name"] = dataframe["name"].map(clean_product_name)
    dataframe["discount"] = dataframe["discount"].map(normalize_discount)
    dataframe["product_id"] = dataframe["product_id"].map(
        lambda value: str(value).strip()
        if pd.notna(value) and str(value).strip().lower() != "nan"
        else None
    )

    if os.path.isfile(OUTPUT_CSV_FILE):
        existing = pd.read_csv(OUTPUT_CSV_FILE)
        if "sku" in existing.columns:
            existing["product_id"] = existing["sku"]
        elif "url" in existing.columns:
            existing["url"] = existing.get(
                "url",
                pd.Series([None] * len(existing)),
            )
            existing["product_id"] = existing["url"].map(extract_sku)

        if "url" in existing.columns:
            existing["name"] = existing["name"].map(clean_product_name)
            existing["brand"] = existing["name"].map(extract_brand)
            category_pairs = existing.get(
                "source_category",
                pd.Series([None] * len(existing)),
            ).map(category_fields)
            existing["category"] = category_pairs.map(
                lambda value: value[0] if value else None
            )
            existing["subcategory"] = category_pairs.map(
                lambda value: value[1] if value else None
            )
            existing["discount"] = existing["discount"].map(normalize_discount)
            existing = existing.reindex(columns=CSV_COLUMNS)

        existing["name"] = existing["name"].map(clean_product_name)
        existing["brand"] = existing["name"].map(extract_brand)
        existing["discount"] = existing["discount"].map(normalize_discount)
        existing["product_id"] = existing["product_id"].map(
            lambda value: str(value).strip()
            if pd.notna(value) and str(value).strip().lower() != "nan"
            else None
        )
        existing = existing.reindex(columns=CSV_COLUMNS)

        if dataframe.empty:
            combined = existing
        else:
            combined = pd.concat(
                [existing, dataframe.reindex(columns=CSV_COLUMNS)],
                ignore_index=True,
            )
        combined = combined.drop_duplicates(
            subset=["product_id"],
            keep="last",
        )
        combined = combined.dropna(subset=["product_id"])
    else:
        combined = dataframe.reindex(columns=CSV_COLUMNS)

    combined = combined.drop_duplicates(
        subset=["product_id"],
        keep="last",
    )
    combined = combined.sort_values(
        ["category", "subcategory", "name"],
        na_position="last",
    )
    combined = combined.reindex(columns=CSV_COLUMNS)
    combined.to_csv(
        OUTPUT_CSV_FILE,
        index=False,
        encoding="utf-8-sig",
    )
    print(
        f"Merged {len(dataframe)} rows into {OUTPUT_CSV_FILE}; "
        f"{len(combined)} unique rows"
    )


def normalize_supabase_row(row):
    record = {}
    integer_columns = {"original_price", "current_price", "review_count"}
    float_columns = {"rating"}

    for column in CSV_COLUMNS:
        value = row.get(column)
        if value is None or pd.isna(value):
            record[column] = None
            continue

        if column in integer_columns:
            try:
                record[column] = int(value)
            except (TypeError, ValueError, OverflowError):
                record[column] = None
            continue

        if column in float_columns:
            try:
                record[column] = float(value)
            except (TypeError, ValueError, OverflowError):
                record[column] = None
            continue

        if column == "scraped_at":
            try:
                timestamp = pd.Timestamp(str(value).strip())
                if timestamp.tzinfo is None:
                    timestamp = timestamp.tz_localize("Asia/Jakarta")
                record[column] = timestamp.isoformat()
            except (TypeError, ValueError, OverflowError):
                record[column] = None
            continue

        text = str(value).strip()
        record[column] = text if text and text.lower() != "nan" else None

    return record


def insert_to_supabase(dataframe):
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")

    if not supabase_url or not supabase_key:
        print(
            "Supabase skipped: SUPABASE_URL and SUPABASE_KEY are not set."
        )
        return 0

    if create_client is None:
        raise RuntimeError(
            "Supabase client is unavailable. Install dependencies from requirements.txt."
        )

    records = [
        normalize_supabase_row(row)
        for row in dataframe.reindex(columns=CSV_COLUMNS).to_dict("records")
        if row.get("product_id") is not None
    ]
    if not records:
        print("Supabase skipped: no records to insert.")
        return 0

    client = create_client(supabase_url, supabase_key)
    batch_size = 500
    for start in range(0, len(records), batch_size):
        batch = records[start:start + batch_size]
        client.table("ruparupa_products").upsert(
            batch,
            on_conflict="product_id",
        ).execute()

    print(f"Upserted {len(records)} rows to Supabase.")
    return len(records)


if __name__ == "__main__":
    products = scrape_ruparupa()
    append_to_csv(products)
    insert_to_supabase(products)
    print(products.head())
