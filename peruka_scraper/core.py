from __future__ import annotations

import csv
import json
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from bs4 import BeautifulSoup


USER_AGENT = "peruka-scraper/0.1 (+https://github.com/JakesterTV/peruka-scraper)"
DEFAULT_SITEMAP_PATHS = (
    "/robots.txt",
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/mapa-strony.xml",
    "/sitemap-products.xml",
    "/sitemap_products.xml",
    "/sitemap_products_0.xml",
    "/sitemap_products_1.xml",
    "/sitemap.xml?type=products",
    "/server-sitemap.xml",
)
PRODUCT_URL_RE = re.compile(r"/[^/?#]+\.html(?:$|[?#])", re.IGNORECASE)
VARIANT_TERMS = ("wybierz", "kolor", "color", "odcie", "wariant", "variant", "swatch")
NON_PRODUCT_PATH_TERMS = ("blog", "aktualnosci", "news", "poradnik", "guide", "article")
PRODUCT_TYPE_COLUMNS = (
    "ID",
    "Type",
    "SKU",
    "Name",
    "Published",
    "Is featured?",
    "Visibility in catalog",
    "Short description",
    "Description",
    "Tax status",
    "In stock?",
    "Regular price",
    "Categories",
    "Images",
    "Parent",
    "Attribute 1 name",
    "Attribute 1 value(s)",
    "Attribute 1 visible",
    "Attribute 1 global",
)


class ScraperError(RuntimeError):
    """Raised when the scraper cannot continue safely."""


@dataclass(slots=True)
class VariantLink:
    url: str
    label: str


@dataclass(slots=True)
class InlineVariant:
    label: str
    sku: str | None
    price: str | None
    availability: str | None
    images: list[str]


@dataclass(slots=True)
class ProductRecord:
    url: str
    canonical_url: str
    title: str
    short_description: str
    description: str
    sku: str | None
    price: str | None
    currency: str | None
    availability: str | None
    images: list[str]
    categories: list[str]
    variant_links: list[VariantLink]
    discovered_colors: list[str]
    inline_variants: list[InlineVariant]


@dataclass(slots=True)
class ProductFamily:
    parent_sku: str
    parent_name: str
    categories: list[str]
    products: list[ProductRecord]
    color_by_url: dict[str, str]


@dataclass(slots=True)
class ScrapeResult:
    products_csv: Path
    images_dir: Path | None
    image_manifest: Path | None
    discovered_products: int
    exported_rows: int
    warnings: list[str] = field(default_factory=list)


class HttpClient:
    def __init__(self, timeout: int = 30) -> None:
        self.timeout = timeout

    def get_text(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=self.timeout) as response:
            content_type = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(content_type, errors="replace")

    def get_bytes(self, url: str) -> bytes:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=self.timeout) as response:
            return response.read()


def scrape_site(
    base_url: str,
    output_dir: Path,
    *,
    seed_product_urls: Iterable[str] | None = None,
    download_images: bool = False,
    max_products: int | None = None,
    client: HttpClient | None = None,
) -> ScrapeResult:
    client = client or HttpClient()
    normalized_base = normalize_base_url(base_url)
    output_dir.mkdir(parents=True, exist_ok=True)

    queue = deque(discover_product_urls(normalized_base, client))
    for seed_url in seed_product_urls or ():
        queue.append(normalize_url(seed_url, normalized_base))

    seen_urls: set[str] = set()
    products: dict[str, ProductRecord] = {}
    warnings: list[str] = []

    while queue and (max_products is None or len(products) < max_products):
        url = normalize_url(queue.popleft(), normalized_base)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            html = client.get_text(url)
            record = parse_product_html(url, html)
        except (HTTPError, URLError, TimeoutError, ScraperError, ValueError) as exc:
            warnings.append(f"Failed to scrape {url}: {exc}")
            continue

        canonical_url = normalize_url(record.canonical_url or url, normalized_base)
        record = ProductRecord(
            url=normalize_url(record.url, normalized_base),
            canonical_url=canonical_url,
            title=record.title,
            short_description=record.short_description,
            description=record.description,
            sku=record.sku,
            price=record.price,
            currency=record.currency,
            availability=record.availability,
            images=record.images,
            categories=record.categories,
            variant_links=[
                VariantLink(normalize_url(link.url, normalized_base), link.label)
                for link in record.variant_links
            ],
            discovered_colors=record.discovered_colors,
            inline_variants=record.inline_variants,
        )
        products[record.url] = record
        if canonical_url != record.url and canonical_url not in products:
            products[canonical_url] = record

        for link in record.variant_links:
            if link.url not in seen_urls:
                queue.append(link.url)

    families = build_families(list(unique_records(products.values())))
    rows = generate_woocommerce_rows(families)
    products_csv = output_dir / "products.csv"
    write_csv(products_csv, rows)

    images_dir: Path | None = None
    image_manifest: Path | None = None
    if download_images:
        images_dir = output_dir / "images"
        image_manifest = output_dir / "images.csv"
        download_product_images(families, images_dir, image_manifest, client, warnings)

    validate_rows(rows)
    return ScrapeResult(
        products_csv=products_csv,
        images_dir=images_dir,
        image_manifest=image_manifest,
        discovered_products=len(families),
        exported_rows=len(rows),
        warnings=warnings,
    )


def normalize_base_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or parsed.path
    return f"{scheme}://{netloc}".rstrip("/")


def normalize_url(url: str, base_url: str) -> str:
    normalized = urljoin(f"{base_url}/", url)
    parts = urlsplit(normalized)
    path = parts.path or "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


def discover_product_urls(base_url: str, client: HttpClient) -> list[str]:
    sitemap_urls: list[str] = []
    robots_url = f"{base_url}/robots.txt"
    try:
        robots_text = client.get_text(robots_url)
        sitemap_urls.extend(parse_robots_sitemaps(robots_text, base_url))
    except (HTTPError, URLError):
        pass

    for path in DEFAULT_SITEMAP_PATHS[1:]:
        sitemap_urls.append(urljoin(base_url, path))

    discovered: set[str] = set()
    product_urls: set[str] = set()
    for sitemap_url in dedupe_preserve_order(sitemap_urls):
        _walk_sitemap(sitemap_url, base_url, client, discovered, product_urls)
    return sorted(product_urls)


def parse_robots_sitemaps(text: str, base_url: str) -> list[str]:
    sitemap_urls: list[str] = []
    for line in text.splitlines():
        if line.lower().startswith("sitemap:"):
            sitemap_urls.append(normalize_url(line.split(":", 1)[1].strip(), base_url))
    return sitemap_urls


def _walk_sitemap(
    sitemap_url: str,
    base_url: str,
    client: HttpClient,
    discovered: set[str],
    product_urls: set[str],
) -> None:
    if sitemap_url in discovered:
        return
    discovered.add(sitemap_url)
    try:
        xml_text = client.get_text(sitemap_url)
    except (HTTPError, URLError):
        return

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return

    namespace = ""
    if root.tag.startswith("{"):
        namespace = root.tag.split("}", 1)[0] + "}"

    if root.tag.endswith("sitemapindex"):
        for location in root.findall(f".//{namespace}loc"):
            if location.text:
                _walk_sitemap(normalize_url(location.text.strip(), base_url), base_url, client, discovered, product_urls)
        return

    if root.tag.endswith("urlset"):
        for location in root.findall(f".//{namespace}loc"):
            if not location.text:
                continue
            url = normalize_url(location.text.strip(), base_url)
            if is_product_url(url, base_url):
                product_urls.add(url)


def is_product_url(url: str, base_url: str) -> bool:
    parsed = urlparse(url)
    base_netloc = urlparse(base_url).netloc
    path_terms = {part.casefold() for part in parsed.path.split("/") if part}
    return (
        parsed.netloc == base_netloc
        and bool(PRODUCT_URL_RE.search(parsed.path))
        and not path_terms.intersection(NON_PRODUCT_PATH_TERMS)
    )


def parse_product_html(url: str, html: str) -> ProductRecord:
    soup = BeautifulSoup(html, "html.parser")
    json_ld_objects = extract_json_ld_objects(soup)
    product_json = choose_product_object(json_ld_objects)
    inline_product = extract_inline_product_data(html)

    title = (
        nested_json_value(product_json, "name")
        or meta_content(soup, property_name="og:title")
        or text_or_none(soup.find("h1"))
        or text_or_none(soup.find("title"))
    )
    if not title:
        raise ScraperError("Missing product title")

    canonical_url = (
        href_or_none(soup.find("link", rel="canonical"))
        or nested_json_value(product_json, "url")
        or url
    )
    description = strip_html(
        nested_json_value(product_json, "description")
        or html_or_none(find_first(soup, '[class*="description"], [id*="description"]'))
        or ""
    )
    short_description = strip_html(
        meta_content(soup, attrs={"name": "description"})
        or description
    )
    images = dedupe_preserve_order(
        [*extract_images_from_json(product_json), *extract_images_from_html(soup, canonical_url)]
    )
    categories = dedupe_preserve_order(extract_categories(soup))
    offers = choose_offer(product_json)
    variant_links = extract_variant_links(soup, canonical_url)
    inline_variants = extract_inline_variants(inline_product, soup, canonical_url)
    discovered_colors = dedupe_preserve_order(
        [link.label for link in variant_links if link.label]
        + [variant.label for variant in inline_variants]
        + extract_select_colors(soup)
    )

    return ProductRecord(
        url=url,
        canonical_url=canonical_url,
        title=clean_text(title),
        short_description=clean_text(short_description),
        description=clean_text(description),
        sku=nested_json_value(product_json, "sku"),
        price=stringify(nested_json_value(offers, "price")),
        currency=stringify(nested_json_value(offers, "priceCurrency")),
        availability=stringify(nested_json_value(offers, "availability")),
        images=images,
        categories=categories,
        variant_links=variant_links,
        discovered_colors=discovered_colors,
        inline_variants=inline_variants,
    )


def extract_json_ld_objects(soup: BeautifulSoup) -> list[dict]:
    objects: list[dict] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not script.string and not script.text:
            continue
        raw_text = script.string or script.text or ""
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            continue
        objects.extend(flatten_json_ld(payload))
    return objects


def extract_inline_product_data(html: str) -> dict:
    patterns = (
        r"var\s+product\s*=\s*(\{.*?\})\s*;",
        r"window\.product\s*=\s*(\{.*?\})\s*;",
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.DOTALL)
        if not match:
            continue
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def flatten_json_ld(payload: object) -> list[dict]:
    if isinstance(payload, list):
        output: list[dict] = []
        for item in payload:
            output.extend(flatten_json_ld(item))
        return output
    if isinstance(payload, dict):
        if "@graph" in payload and isinstance(payload["@graph"], list):
            output = [payload]
            for item in payload["@graph"]:
                output.extend(flatten_json_ld(item))
            return output
        return [payload]
    return []


def choose_product_object(objects: list[dict]) -> dict:
    for item in objects:
        item_type = stringify(item.get("@type")) or ""
        if "Product" in item_type:
            return item
    return {}


def choose_offer(product_json: dict) -> dict:
    offers = product_json.get("offers")
    if isinstance(offers, list) and offers:
        first_offer = offers[0]
        return first_offer if isinstance(first_offer, dict) else {}
    return offers if isinstance(offers, dict) else {}


def extract_images_from_json(product_json: dict) -> list[str]:
    image_field = product_json.get("image", [])
    if isinstance(image_field, str):
        return [image_field]
    if isinstance(image_field, list):
        return [item for item in image_field if isinstance(item, str)]
    return []


def extract_images_from_html(soup: BeautifulSoup, base_url: str) -> list[str]:
    urls: list[str] = []
    selectors = [
        ('meta[property="og:image"]', "content"),
        ("img", "src"),
        ("img", "data-src"),
        ("a", "href"),
    ]
    for selector, attr in selectors:
        for element in soup.select(selector):
            value = element.get(attr)
            if not value:
                continue
            if looks_like_image_url(value):
                urls.append(urljoin(base_url, value))
    return urls


def extract_categories(soup: BeautifulSoup) -> list[str]:
    breadcrumb_selectors = (
        '[itemtype*="BreadcrumbList"] a',
        ".breadcrumbs a",
        ".breadcrumb a",
    )
    categories: list[str] = []
    for selector in breadcrumb_selectors:
        categories.extend(
            clean_text(element.get_text(" ", strip=True))
            for element in soup.select(selector)
            if clean_text(element.get_text(" ", strip=True))
        )
    if len(categories) > 1:
        return [category for category in categories if category.lower() not in {"start", "home"}]
    return []


def extract_inline_variants(product_data: dict, soup: BeautifulSoup, current_url: str) -> list[InlineVariant]:
    if not isinstance(product_data, dict):
        return []

    color_positions = detect_color_positions(product_data)
    stock_image_map = build_stock_image_map(soup, current_url)
    variants: list[InlineVariant] = []
    for stock in product_data.get("stocks", []) or []:
        if not isinstance(stock, dict):
            continue
        label = extract_stock_color_label(stock, color_positions)
        if not label:
            continue
        images = extract_stock_images(stock, stock_image_map)
        variants.append(
            InlineVariant(
                label=label,
                sku=stringify(stock.get("code") or stock.get("sku") or stock.get("stock_id")),
                price=stringify(stock.get("price")),
                availability="instock" if int_or_none(stock.get("stock") or stock.get("quantity")) not in {None, 0} else None,
                images=images,
            )
        )
    deduped: dict[str, InlineVariant] = {}
    for variant in variants:
        key = variant.label.casefold()
        if key not in deduped:
            deduped[key] = variant
    return list(deduped.values())


def extract_variant_links(soup: BeautifulSoup, current_url: str) -> list[VariantLink]:
    current_netloc = urlparse(current_url).netloc
    candidates: list[VariantLink] = []

    containers = [
        tag for tag in soup.find_all(True)
        if looks_like_variant_container(tag)
    ]
    if not containers:
        containers = [soup]

    for container in containers:
        for anchor in container.find_all("a", href=True):
            href = anchor.get("href")
            if not href:
                continue
            absolute_url = urljoin(current_url, href)
            parsed = urlparse(absolute_url)
            if parsed.netloc != current_netloc or not PRODUCT_URL_RE.search(parsed.path):
                continue
            label = extract_variant_label(anchor)
            if not label and container is soup:
                continue
            candidates.append(VariantLink(url=absolute_url, label=label or infer_label_from_url(absolute_url)))

    return [
        VariantLink(url=url, label=label)
        for url, label in dedupe_variant_links(candidates).items()
    ]


def detect_color_positions(product_data: dict) -> list[int]:
    color_positions: list[int] = []
    gauges = product_data.get("gauges", []) or []
    for index, gauge in enumerate(gauges, start=1):
        if not isinstance(gauge, dict):
            continue
        names = [
            stringify(gauge.get("name")) or "",
            stringify(nested_json_value(gauge.get("translations", {}).get("pl_PL", {}), "name")) or "",
            stringify(gauge.get("label")) or "",
        ]
        if any(any(term in name.lower() for term in VARIANT_TERMS) for name in names if name):
            color_positions.append(index)
    return color_positions


def build_stock_image_map(soup: BeautifulSoup, current_url: str) -> dict[str, list[str]]:
    image_map: dict[str, list[str]] = {}
    for element in soup.select("[data-stock-id]"):
        stock_id = clean_text(element.get("data-stock-id", ""))
        if not stock_id:
            continue
        urls = []
        for value in (
            element.get("data-image"),
            element.get("data-src"),
            element.get("src"),
            element.get("href"),
        ):
            if not value:
                continue
            absolute_url = urljoin(current_url, value)
            if looks_like_image_url(absolute_url):
                urls.append(absolute_url)
        if urls:
            image_map[stock_id] = dedupe_preserve_order(urls)
    return image_map


def extract_stock_color_label(stock: dict, color_positions: list[int]) -> str:
    for position in color_positions:
        for key in (f"gvalue{position}", f"value{position}", f"option{position}"):
            label = clean_text(stringify(stock.get(key)) or "")
            if label:
                return label
    for key, value in stock.items():
        if key.startswith(("gvalue", "value")) and clean_text(stringify(value) or ""):
            return clean_text(stringify(value) or "")
    return ""


def extract_stock_images(stock: dict, stock_image_map: dict[str, list[str]]) -> list[str]:
    image_candidates = [
        stringify(stock.get("image")),
        stringify(stock.get("photo")),
        stringify(stock.get("icon")),
    ]
    stock_id = stringify(stock.get("stock_id"))
    if stock_id and stock_id in stock_image_map:
        image_candidates.extend(stock_image_map[stock_id])
    return dedupe_preserve_order([image for image in image_candidates if image])


def looks_like_variant_container(tag) -> bool:
    combined = " ".join(
        filter(
            None,
            [
                tag.get("id"),
                " ".join(tag.get("class", [])),
                tag.get_text(" ", strip=True)[:120],
            ],
        )
    ).lower()
    return any(term in combined for term in VARIANT_TERMS)


def extract_variant_label(anchor) -> str:
    attribute_candidates = (
        anchor.get("title"),
        anchor.get("aria-label"),
        anchor.get("data-name"),
        anchor.get("data-option"),
        anchor.get("data-value"),
        anchor.get("data-color"),
    )
    for candidate in attribute_candidates:
        if candidate and clean_text(candidate):
            return clean_text(candidate)
    text = clean_text(anchor.get_text(" ", strip=True))
    if text:
        return text
    image = anchor.find("img")
    if image:
        alt_text = clean_text(image.get("alt", ""))
        if alt_text:
            return alt_text
    return ""


def infer_label_from_url(url: str) -> str:
    slug = Path(urlparse(url).path).stem
    label = slug.rsplit("-", 1)[-1].replace("-", " ")
    return clean_text(label)


def dedupe_variant_links(links: list[VariantLink]) -> dict[str, str]:
    deduped: dict[str, str] = {}
    for link in links:
        normalized_url = strip_query_fragment(link.url)
        label = clean_text(link.label)
        if normalized_url not in deduped or (label and len(label) > len(deduped[normalized_url])):
            deduped[normalized_url] = label
    return deduped


def extract_select_colors(soup: BeautifulSoup) -> list[str]:
    colors: list[str] = []
    for select in soup.find_all("select"):
        combined = " ".join(filter(None, [select.get("name"), select.get("id"), " ".join(select.get("class", []))])).lower()
        if not any(term in combined for term in VARIANT_TERMS):
            continue
        for option in select.find_all("option"):
            label = clean_text(option.get_text(" ", strip=True))
            if label and label.lower() not in {"wybierz", "select", "choose"}:
                colors.append(label)
    return colors


def build_families(records: list[ProductRecord]) -> list[ProductFamily]:
    by_url = {record.url: record for record in records}
    graph: dict[str, set[str]] = {record.url: set() for record in records}
    labels_by_url: dict[str, list[str]] = {record.url: [] for record in records}

    for record in records:
        for link in record.variant_links:
            if link.url not in by_url:
                continue
            graph[record.url].add(link.url)
            graph[link.url].add(record.url)
            if link.label:
                labels_by_url[link.url].append(link.label)

    families: list[ProductFamily] = []
    visited: set[str] = set()
    for url in graph:
        if url in visited:
            continue
        stack = [url]
        component: list[ProductRecord] = []
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            component.append(by_url[current])
            stack.extend(graph[current] - visited)

        color_by_url = infer_colors(component, labels_by_url)
        categories = dedupe_preserve_order(
            category for product in component for category in product.categories
        )
        parent_name = infer_parent_name(component, color_by_url)
        parent_sku = infer_parent_sku(component, parent_name)
        families.append(
            ProductFamily(
                parent_sku=parent_sku,
                parent_name=parent_name,
                categories=categories,
                products=sorted(component, key=lambda item: color_by_url.get(item.url, item.title)),
                color_by_url=color_by_url,
            )
        )
    return sorted(families, key=lambda family: family.parent_name.lower())


def infer_colors(records: list[ProductRecord], labels_by_url: dict[str, list[str]]) -> dict[str, str]:
    color_by_url: dict[str, str] = {}
    all_titles = [record.title for record in records]
    shared_prefix = shared_word_prefix(all_titles)
    for record in records:
        labels = dedupe_preserve_order(labels_by_url.get(record.url, []) + record.discovered_colors)
        if labels:
            color_by_url[record.url] = labels[0]
            continue
        inferred = clean_text(record.title.removeprefix(shared_prefix).strip(" -/|")) if shared_prefix else ""
        color_by_url[record.url] = inferred or "Default"
    return color_by_url


def infer_parent_name(records: list[ProductRecord], color_by_url: dict[str, str]) -> str:
    candidates: list[str] = []
    for record in records:
        color = color_by_url.get(record.url, "")
        if color and record.title.lower().endswith(color.lower()):
            candidates.append(clean_text(record.title[: -len(color)].strip(" -/|")))
    if candidates:
        return Counter(candidates).most_common(1)[0][0]
    shared_prefix = shared_word_prefix([record.title for record in records])
    return shared_prefix or records[0].title


def infer_parent_sku(records: list[ProductRecord], parent_name: str) -> str:
    sku_roots = [record.sku for record in records if record.sku]
    if sku_roots:
        base = re.sub(r"[-_ ]?[A-Za-z0-9]+$", "", sku_roots[0] or "").strip("-_ ")
        if base:
            return base
    return slugify(parent_name)


def shared_word_prefix(values: list[str]) -> str:
    tokenized = [clean_text(value).split() for value in values if clean_text(value)]
    if not tokenized:
        return ""
    prefix: list[str] = []
    for group in zip(*tokenized):
        if len(set(map(str.lower, group))) == 1:
            prefix.append(group[0])
        else:
            break
    return " ".join(prefix).strip()


def generate_woocommerce_rows(families: list[ProductFamily]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for family in families:
        colors = dedupe_preserve_order(family.color_by_url[product.url] for product in family.products)
        inline_variants = family.products[0].inline_variants if len(family.products) == 1 else []
        if len(family.products) > 1 or len(colors) > 1 or len(inline_variants) > 1:
            parent_product = family.products[0]
            attribute_values = colors or [variant.label for variant in inline_variants]
            rows.append(
                make_row(
                    product_type="variable",
                    sku=family.parent_sku,
                    name=family.parent_name,
                    short_description=parent_product.short_description,
                    description=parent_product.description,
                    in_stock=availability_to_stock(parent_product.availability),
                    regular_price="",
                    categories=family.categories,
                    images=parent_product.images,
                    parent="",
                    attribute_value=", ".join(attribute_values),
                )
            )
            if len(family.products) == 1 and len(inline_variants) > 1:
                base_product = family.products[0]
                for variant in inline_variants:
                    rows.append(
                        make_row(
                            product_type="variation",
                            sku=variant.sku or f"{family.parent_sku}-{slugify(variant.label)}",
                            name="",
                            short_description="",
                            description="",
                            in_stock=availability_to_stock(variant.availability or base_product.availability),
                            regular_price=variant.price or base_product.price or "",
                            categories=[],
                            images=variant.images or base_product.images,
                            parent=family.parent_sku,
                            attribute_value=variant.label,
                        )
                    )
            else:
                for product in family.products:
                    color = family.color_by_url[product.url]
                    rows.append(
                        make_row(
                            product_type="variation",
                            sku=product.sku or f"{family.parent_sku}-{slugify(color)}",
                            name="",
                            short_description="",
                            description="",
                            in_stock=availability_to_stock(product.availability),
                            regular_price=product.price or "",
                            categories=[],
                            images=product.images,
                            parent=family.parent_sku,
                            attribute_value=color,
                        )
                    )
        else:
            product = family.products[0]
            rows.append(
                make_row(
                    product_type="simple",
                    sku=product.sku or family.parent_sku,
                    name=product.title,
                    short_description=product.short_description,
                    description=product.description,
                    in_stock=availability_to_stock(product.availability),
                    regular_price=product.price or "",
                    categories=family.categories,
                    images=product.images,
                    parent="",
                    attribute_value="",
                )
            )
    return rows


def make_row(
    *,
    product_type: str,
    sku: str,
    name: str,
    short_description: str,
    description: str,
    in_stock: str,
    regular_price: str,
    categories: list[str],
    images: list[str],
    parent: str,
    attribute_value: str,
) -> dict[str, str]:
    row = {column: "" for column in PRODUCT_TYPE_COLUMNS}
    row.update(
        {
            "Type": product_type,
            "SKU": sku,
            "Name": name,
            "Published": "1",
            "Is featured?": "0",
            "Visibility in catalog": "visible" if product_type != "variation" else "",
            "Short description": short_description,
            "Description": description,
            "Tax status": "taxable",
            "In stock?": in_stock,
            "Regular price": regular_price,
            "Categories": ", ".join(categories),
            "Images": ", ".join(images),
            "Parent": parent,
            "Attribute 1 name": "Color" if product_type != "simple" or attribute_value else "",
            "Attribute 1 value(s)": attribute_value,
            "Attribute 1 visible": "1" if product_type != "variation" and attribute_value else ("1" if product_type == "variation" else ""),
            "Attribute 1 global": "1" if attribute_value else "",
        }
    )
    return row


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PRODUCT_TYPE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def download_product_images(
    families: list[ProductFamily],
    images_dir: Path,
    manifest_path: Path,
    client: HttpClient,
    warnings: list[str],
) -> None:
    images_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for family in families:
        for product in family.products:
            color = family.color_by_url.get(product.url, "default")
            product_slug = slugify(f"{family.parent_name}-{color}")
            for index, image_url in enumerate(product.images, start=1):
                if image_url in seen_urls:
                    continue
                seen_urls.add(image_url)
                suffix = Path(urlparse(image_url).path).suffix or ".jpg"
                filename = f"{product_slug}-{index}{suffix}"
                destination = images_dir / filename
                try:
                    destination.write_bytes(client.get_bytes(image_url))
                except (HTTPError, URLError, TimeoutError) as exc:
                    warnings.append(f"Failed to download image {image_url}: {exc}")
                    continue
                manifest_rows.append(
                    {
                        "product_url": product.url,
                        "color": color,
                        "image_url": image_url,
                        "local_path": str(destination),
                    }
                )
    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["product_url", "color", "image_url", "local_path"])
        writer.writeheader()
        writer.writerows(manifest_rows)


def validate_rows(rows: list[dict[str, str]]) -> None:
    if not rows:
        raise ScraperError("No WooCommerce rows were generated.")

    parent_skus = {row["SKU"] for row in rows if row["Type"] in {"variable", "simple"}}
    seen_skus: set[str] = set()
    for row in rows:
        sku = row["SKU"]
        if not sku:
            raise ScraperError("Encountered a row without SKU.")
        if sku in seen_skus:
            raise ScraperError(f"Duplicate SKU generated: {sku}")
        seen_skus.add(sku)
        if row["Type"] == "variation" and row["Parent"] not in parent_skus:
            raise ScraperError(f"Variation SKU {sku} is missing a valid parent SKU.")
        if row["Images"]:
            for image_url in [item.strip() for item in row["Images"].split(",") if item.strip()]:
                parsed = urlparse(image_url)
                if parsed.scheme not in {"http", "https"}:
                    raise ScraperError(f"Invalid image URL: {image_url}")


def dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        cleaned = clean_text(value)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
    return output


def unique_records(records: Iterable[ProductRecord]) -> list[ProductRecord]:
    seen_urls: set[str] = set()
    output: list[ProductRecord] = []
    for record in records:
        if record.url in seen_urls:
            continue
        seen_urls.add(record.url)
        output.append(record)
    return output


def meta_content(soup: BeautifulSoup, property_name: str | None = None, attrs: dict | None = None) -> str:
    search_attrs = attrs.copy() if attrs else {}
    if property_name:
        search_attrs["property"] = property_name
    tag = soup.find("meta", attrs=search_attrs)
    return tag.get("content", "") if tag else ""


def find_first(soup: BeautifulSoup, selector: str):
    return soup.select_one(selector)


def text_or_none(tag) -> str:
    if not tag:
        return ""
    return tag.get_text(" ", strip=True)


def html_or_none(tag) -> str:
    if not tag:
        return ""
    return tag.decode_contents()


def href_or_none(tag) -> str:
    if not tag:
        return ""
    return tag.get("href", "")


def nested_json_value(payload: dict, key: str):
    if not isinstance(payload, dict):
        return None
    return payload.get(key)


def stringify(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return clean_text(value)
    return str(value)


def int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def strip_html(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return soup.get_text(" ", strip=True)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def availability_to_stock(value: str | None) -> str:
    normalized = (value or "").lower()
    return "1" if any(token in normalized for token in ("instock", "in stock", "available")) else "0"


def looks_like_image_url(url: str) -> bool:
    return url.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


def slugify(value: str) -> str:
    ascii_value = re.sub(r"[^a-zA-Z0-9]+", "-", clean_text(value).lower())
    return ascii_value.strip("-") or "product"


def strip_query_fragment(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"
