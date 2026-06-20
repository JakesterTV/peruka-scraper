from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

from peruka_scraper.core import (
    HttpClient,
    InlineVariant,
    ProductRecord,
    VariantLink,
    build_families,
    discover_product_urls,
    generate_woocommerce_rows,
    parse_product_html,
    scrape_site,
)


class FakeClient(HttpClient):
    def __init__(self, responses: dict[str, str | bytes]) -> None:
        super().__init__(timeout=1)
        self.responses = responses

    def get_text(self, url: str) -> str:
        if url not in self.responses:
            raise URLError(f"Missing fixture for {url}")
        response = self.responses[url]
        if isinstance(response, bytes):
            return response.decode("utf-8")
        return response

    def get_bytes(self, url: str) -> bytes:
        if url not in self.responses:
            raise URLError(f"Missing fixture for {url}")
        response = self.responses[url]
        if isinstance(response, bytes):
            return response
        return response.encode("utf-8")


PRODUCT_ONE = """
<html>
  <head>
    <title>UMA S 6/8 Blonde</title>
    <link rel="canonical" href="https://shop.example.com/uma-s-6-8-blonde.html" />
    <meta property="og:image" content="https://cdn.example.com/blonde.jpg" />
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "UMA S 6/8 Blonde",
        "sku": "UMA-S-6-8-BLONDE",
        "image": ["https://cdn.example.com/blonde.jpg"],
        "description": "<p>Natural wig.</p>",
        "offers": {
          "@type": "Offer",
          "price": "1999.00",
          "priceCurrency": "PLN",
          "availability": "https://schema.org/InStock"
        }
      }
    </script>
    <script>
      var product = {
        "gauges": [{"name": "Kolor"}],
        "stocks": [
          {"stock_id": 1, "code": "UMA-S-6-8-BLONDE", "gvalue1": "Blonde", "price": "1999.00", "stock": 5},
          {"stock_id": 2, "code": "UMA-S-6-8-BRUNETTE", "gvalue1": "Brunette", "price": "1999.00", "stock": 3}
        ]
      };
    </script>
  </head>
  <body>
    <nav class="breadcrumbs"><a>Home</a><a>Wigs</a><a>Natural</a></nav>
    <section class="variant-picker">
      <h2>Wybierz kolor</h2>
      <a href="/uma-s-6-8-blonde.html" title="Blonde"><img alt="Blonde" src="https://cdn.example.com/blonde.jpg" /></a>
      <a href="/uma-s-6-8-brunette.html" title="Brunette"><img alt="Brunette" src="https://cdn.example.com/brunette.jpg" /></a>
    </section>
  </body>
</html>
"""


PRODUCT_TWO = """
<html>
  <head>
    <title>UMA S 6/8 Brunette</title>
    <link rel="canonical" href="https://shop.example.com/uma-s-6-8-brunette.html" />
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "UMA S 6/8 Brunette",
        "sku": "UMA-S-6-8-BRUNETTE",
        "image": ["https://cdn.example.com/brunette.jpg"],
        "offers": {
          "@type": "Offer",
          "price": "1999.00",
          "priceCurrency": "PLN",
          "availability": "https://schema.org/InStock"
        }
      }
    </script>
  </head>
  <body>
    <section class="variant-picker">
      <h2>Wybierz kolor</h2>
      <a href="/uma-s-6-8-blonde.html" title="Blonde">Blonde</a>
      <a href="/uma-s-6-8-brunette.html" title="Brunette">Brunette</a>
    </section>
  </body>
</html>
"""


class CoreTests(unittest.TestCase):
    def test_parse_product_html_extracts_variant_links(self) -> None:
        product = parse_product_html("https://shop.example.com/uma-s-6-8-blonde.html", PRODUCT_ONE)
        self.assertEqual(product.title, "UMA S 6/8 Blonde")
        self.assertEqual(product.sku, "UMA-S-6-8-BLONDE")
        self.assertEqual(product.price, "1999.00")
        self.assertEqual(product.categories, ["Wigs", "Natural"])
        self.assertEqual(
            [(variant.url, variant.label) for variant in product.variant_links],
            [
                ("https://shop.example.com/uma-s-6-8-blonde.html", "Blonde"),
                ("https://shop.example.com/uma-s-6-8-brunette.html", "Brunette"),
            ],
        )
        self.assertEqual([variant.label for variant in product.inline_variants], ["Blonde", "Brunette"])

    def test_discover_product_urls_follows_sitemap_index(self) -> None:
        client = FakeClient(
            {
                "https://shop.example.com/robots.txt": "Sitemap: https://shop.example.com/sitemap.xml\n",
                "https://shop.example.com/sitemap.xml": """
                    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                      <sitemap><loc>https://shop.example.com/products.xml</loc></sitemap>
                    </sitemapindex>
                """,
                "https://shop.example.com/sitemap_index.xml": "",
                "https://shop.example.com/mapa-strony.xml": "",
                "https://shop.example.com/sitemap-products.xml": "",
                "https://shop.example.com/sitemap_products.xml": "",
                "https://shop.example.com/server-sitemap.xml": "",
                "https://shop.example.com/products.xml": """
                    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                      <url><loc>https://shop.example.com/uma-s-6-8-blonde.html</loc></url>
                      <url><loc>https://shop.example.com/blog/post.html</loc></url>
                    </urlset>
                """,
            }
        )
        urls = discover_product_urls("https://shop.example.com", client)
        self.assertEqual(urls, ["https://shop.example.com/uma-s-6-8-blonde.html"])

    def test_build_families_and_generate_rows(self) -> None:
        records = [
            ProductRecord(
                url="https://shop.example.com/uma-s-6-8-blonde.html",
                canonical_url="https://shop.example.com/uma-s-6-8-blonde.html",
                title="UMA S 6/8 Blonde",
                short_description="Short",
                description="Long",
                sku="UMA-S-6-8-BLONDE",
                price="1999.00",
                currency="PLN",
                availability="https://schema.org/InStock",
                images=["https://cdn.example.com/blonde.jpg"],
                categories=["Wigs"],
                variant_links=[VariantLink("https://shop.example.com/uma-s-6-8-brunette.html", "Brunette")],
                discovered_colors=["Blonde", "Brunette"],
                inline_variants=[],
            ),
            ProductRecord(
                url="https://shop.example.com/uma-s-6-8-brunette.html",
                canonical_url="https://shop.example.com/uma-s-6-8-brunette.html",
                title="UMA S 6/8 Brunette",
                short_description="Short",
                description="Long",
                sku="UMA-S-6-8-BRUNETTE",
                price="1999.00",
                currency="PLN",
                availability="https://schema.org/InStock",
                images=["https://cdn.example.com/brunette.jpg"],
                categories=["Wigs"],
                variant_links=[VariantLink("https://shop.example.com/uma-s-6-8-blonde.html", "Blonde")],
                discovered_colors=["Brunette", "Blonde"],
                inline_variants=[],
            ),
        ]
        families = build_families(records)
        rows = generate_woocommerce_rows(families)
        self.assertEqual(rows[0]["Type"], "variable")
        self.assertEqual(rows[0]["Name"], "UMA S 6/8")
        self.assertEqual(rows[1]["Parent"], rows[0]["SKU"])
        self.assertEqual(rows[2]["Parent"], rows[0]["SKU"])

    def test_scrape_site_writes_csv(self) -> None:
        client = FakeClient(
            {
                "https://shop.example.com/robots.txt": "Sitemap: https://shop.example.com/sitemap.xml\n",
                "https://shop.example.com/sitemap.xml": """
                    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                      <url><loc>https://shop.example.com/uma-s-6-8-blonde.html</loc></url>
                    </urlset>
                """,
                "https://shop.example.com/sitemap_index.xml": "",
                "https://shop.example.com/mapa-strony.xml": "",
                "https://shop.example.com/sitemap-products.xml": "",
                "https://shop.example.com/sitemap_products.xml": "",
                "https://shop.example.com/server-sitemap.xml": "",
                "https://shop.example.com/uma-s-6-8-blonde.html": PRODUCT_ONE,
                "https://shop.example.com/uma-s-6-8-brunette.html": PRODUCT_TWO,
            }
        )
        with tempfile.TemporaryDirectory() as tempdir:
            result = scrape_site("https://shop.example.com", Path(tempdir), client=client)
            self.assertTrue(result.products_csv.exists())
            with result.products_csv.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["Type"], "variable")

    def test_inline_variants_generate_variation_rows_without_links(self) -> None:
        record = ProductRecord(
            url="https://shop.example.com/uma-s-6-8.html",
            canonical_url="https://shop.example.com/uma-s-6-8.html",
            title="UMA S 6/8",
            short_description="Short",
            description="Long",
            sku="UMA-S-6-8",
            price="1999.00",
            currency="PLN",
            availability="https://schema.org/InStock",
            images=["https://cdn.example.com/base.jpg"],
            categories=["Wigs"],
            variant_links=[],
            discovered_colors=["Blonde", "Brunette"],
            inline_variants=[
                InlineVariant("Blonde", "UMA-S-6-8-BLONDE", "1999.00", "instock", ["https://cdn.example.com/blonde.jpg"]),
                InlineVariant("Brunette", "UMA-S-6-8-BRUNETTE", "2099.00", "instock", ["https://cdn.example.com/brunette.jpg"]),
            ],
        )
        family = build_families([record])[0]
        rows = generate_woocommerce_rows([family])
        self.assertEqual([row["Type"] for row in rows], ["variable", "variation", "variation"])
        self.assertEqual(rows[1]["Regular price"], "1999.00")
        self.assertEqual(rows[2]["Images"], "https://cdn.example.com/brunette.jpg")


if __name__ == "__main__":
    unittest.main()
