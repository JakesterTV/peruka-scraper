from __future__ import annotations

import argparse
from pathlib import Path

from .core import scrape_site


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape a Shoper-style storefront and export WooCommerce CSV data."
    )
    parser.add_argument("--base-url", required=True, help="Store base URL, e.g. https://www.peruka.pl")
    parser.add_argument(
        "--seed-product-url",
        action="append",
        default=[],
        help="Optional product URL to seed discovery when sitemap coverage is incomplete.",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory where products.csv and optional images will be written.",
    )
    parser.add_argument(
        "--download-images",
        action="store_true",
        help="Download discovered product images into output/images and write an images manifest.",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        default=None,
        help="Optional scrape cap for trial runs.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    result = scrape_site(
        args.base_url,
        Path(args.output_dir),
        seed_product_urls=args.seed_product_url,
        download_images=args.download_images,
        max_products=args.max_products,
    )
    print(f"CSV: {result.products_csv}")
    if result.images_dir:
        print(f"Images: {result.images_dir}")
    if result.image_manifest:
        print(f"Image manifest: {result.image_manifest}")
    print(f"Product families: {result.discovered_products}")
    print(f"Exported rows: {result.exported_rows}")
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
