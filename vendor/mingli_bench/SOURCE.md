# Vendored Source: MingLi-Bench

This directory contains a **vendored / reference copy** of the
[MingLi-Bench](https://github.com/hjun3959-blip/MingLi-Bench) project. It is
included to give the 八字 / 神算子 (BaZi / fortune-telling) feature of this
Telegram bot a local foundation of documentation, datasets, and reference
library code.

The copy is **reference material only** — it is not imported or executed by the
bot at runtime. Bot logic was not modified as part of this vendoring.

## Source

| Field | Value |
|-------|-------|
| Upstream repository | https://github.com/hjun3959-blip/MingLi-Bench |
| Branch | `main` |
| Commit SHA | `b7433280fd86d7a7c27debbc47d0303c218f0bfd` |
| License | MIT (see `source/LICENSE` and `../NOTICE.md`) |
| Copy date | 2026-06-11 |
| Copied into | `vendor/mingli_bench/source/` |

## Layout

```
vendor/mingli_bench/
├── SOURCE.md          # this file
├── NOTICE.md          # license attribution + provenance
└── source/            # files copied verbatim from upstream
    ├── LICENSE
    ├── README.md
    ├── README_zh.md
    ├── requirements.txt
    ├── .env.example   # placeholder keys only — contains NO secrets
    ├── data/
    │   ├── data.json
    │   ├── fortune_api_results.json
    │   └── raw/{2022,2023,2024,2025}.txt
    ├── docs/          # human-readable assets only (see omissions below)
    │   ├── favicon.ico
    │   ├── logo*.png
    │   ├── images/...
    │   └── logos/...
    └── mingli_bench/  # the upstream Python package (reference library)
```

## What was copied

- **Documentation**: `README.md`, `README_zh.md` (English + Chinese).
- **Datasets**: `data/data.json`, `data/fortune_api_results.json`, and the raw
  yearly text corpora under `data/raw/`.
- **Reference library**: the full `mingli_bench` Python package (benchmark,
  data loader/schema, model clients, utils, CLI).
- **Docs assets**: human-readable images, logos, and favicon from the upstream
  static site.
- **Build/config**: `requirements.txt`, `.env.example` (placeholder values only).

## What was intentionally omitted

| Path | Reason |
|------|--------|
| `docs/_next/**` (~4.6 MB of JS/CSS/font chunks) | Next.js **generated build artifacts**, not human-readable docs. Noisy and non-authoritative. |
| `docs/index.html`, `docs/index.txt`, `docs/404.html`, `docs/.nojekyll` | Next.js RSC payloads / static-export scaffolding (machine-generated, not prose). |
| `.gitignore` | Upstream ignore rules are not useful in this repo. |

No secrets were copied. `.env.example` was inspected and contains only
placeholder values (e.g. `OPENAI_API_KEY=your_openai_api_key`).

## Verification performed at copy time

- `python3 -m json.tool` confirmed `data/data.json` and
  `data/fortune_api_results.json` are valid JSON.
- `python3 -m py_compile` succeeded on the entire `mingli_bench` package.

## Updating this vendored copy

To refresh, re-clone upstream at the desired commit, repeat the copy of the
paths listed above (omitting `docs/_next` and the Next.js scaffolding), update
the **Commit SHA** / **Copy date** here, and regenerate the manifest in
`NOTICE.md`.
