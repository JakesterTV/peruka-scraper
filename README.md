# peruka-scraper

Scrapes Shoper-style product pages, groups color variants, and exports a WooCommerce-ready CSV.

## Features

- Discovers products from `robots.txt` and common sitemap/XML endpoints first
- Falls back to product-page HTML parsing for color variant links
- Groups linked color variants into WooCommerce variable products
- Exports `products.csv`
- Optionally downloads product images and writes `images.csv`

## Install

```bash
cd <project-directory>
python -m pip install -e .
```

## Usage

```bash
cd <project-directory>
python -m peruka_scraper.cli \
  --base-url https://www.peruka.pl \
  --seed-product-url https://www.peruka.pl/uma-s-6-8-peruka-naturalna.html \
  --output-dir output \
  --download-images
```

### Output

- `output/products.csv`
- `output/images/` when `--download-images` is enabled
- `output/images.csv` image manifest when `--download-images` is enabled

## Notes

- The scraper prefers XML/sitemap sources because they are faster and more stable than full HTML crawling.
- Variant grouping assumes color is the only variant dimension and uses linked product pages found in the “wybierz”/color picker UI.
- WooCommerce image fields use remote image URLs in the CSV; optional downloads are saved separately for manual use.

## Tests

```bash
cd <project-directory>
python -m unittest discover -s tests -v
```