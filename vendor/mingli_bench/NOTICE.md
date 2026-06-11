# NOTICE — Vendored MingLi-Bench

This Telegram bot bundles a vendored copy of third-party material. This notice
records its provenance and license obligations.

## Provenance

| Field | Value |
|-------|-------|
| Component | MingLi-Bench |
| Upstream | https://github.com/hjun3959-blip/MingLi-Bench |
| Branch | `main` |
| Commit SHA | `b7433280fd86d7a7c27debbc47d0303c218f0bfd` |
| Copy date | 2026-06-11 |
| Vendored at | `vendor/mingli_bench/source/` |

## License

MingLi-Bench is distributed under the **MIT License**. The upstream license
file is preserved verbatim at `vendor/mingli_bench/source/LICENSE`. Its full
text is reproduced below to satisfy the MIT requirement that the copyright and
permission notice be included in all copies or substantial portions.

```
MIT License

Copyright (c) 2026 MingLi-Bench Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Omissions

The Next.js generated static-site artifacts (`docs/_next/**`, `docs/index.html`,
`docs/index.txt`, `docs/404.html`, `docs/.nojekyll`) were **not** vendored.
They are machine-generated build output (~4.6 MB) with no human-readable value.
See `SOURCE.md` for the full rationale.

No secrets were copied. The vendored `.env.example` contains placeholder values
only.

## File manifest

Paths are relative to `vendor/mingli_bench/source/`. SHA-256 truncated to 16 hex chars.

| SHA-256 (short) | Bytes | Path |
|-----------------|-------|------|
| `425d491187f0125c` | 1128 | `.env.example` |
| `2163a239e9b1e8b6` | 1081 | `LICENSE` |
| `64badf3e06546d94` | 6751 | `README.md` |
| `0347b6bca050c94a` | 8237 | `README_zh.md` |
| `528240929b238596` | 159402 | `data/data.json` |
| `e44ff5201486dc19` | 914720 | `data/fortune_api_results.json` |
| `59b7e209fd0ecd49` | 7647 | `data/raw/2022.txt` |
| `cc6720d562a736b3` | 7701 | `data/raw/2023.txt` |
| `61b3a23c826a1439` | 7832 | `data/raw/2024.txt` |
| `a215ba8a534c0135` | 9352 | `data/raw/2025.txt` |
| `c6e2f2d6ea63127e` | 5677 | `docs/favicon.ico` |
| `a73f37fb5125d5f4` | 82293 | `docs/images/posts/Mingli-Bench/uncertainty.png` |
| `006c79e2373270d0` | 19322 | `docs/logo-dark.png` |
| `c8faf6d069e17afe` | 17384 | `docs/logo-light.png` |
| `c8faf6d069e17afe` | 17384 | `docs/logo.png` |
| `a3101f3047a119aa` | 1696 | `docs/logos/claude-color.svg` |
| `deba5f98a5c1796e` | 2164 | `docs/logos/deepseek-color.svg` |
| `3cd31ba03ae44b8c` | 1153 | `docs/logos/doubao.svg` |
| `8ab0a9bafec11f7e` | 2836 | `docs/logos/gemini-color.svg` |
| `9175fc90c2265516` | 756 | `docs/logos/grok.svg` |
| `ea401838aa1d5cc2` | 1148 | `docs/logos/human.svg` |
| `d5233dbde7cd3c9a` | 1270 | `docs/logos/kimi-text.svg` |
| `006c79e2373270d0` | 19322 | `docs/logos/logo-dark.png` |
| `c8faf6d069e17afe` | 17384 | `docs/logos/logo-light.png` |
| `7f7187fa6d9b341a` | 1570 | `docs/logos/minimax-color.svg` |
| `a595df6b423920c6` | 1687 | `docs/logos/openai.svg` |
| `77f5768c66d08ce1` | 2044 | `docs/logos/qwen-color.svg` |
| `0f054052f78ca03c` | 267 | `mingli_bench/__init__.py` |
| `561f7cac5a781ad8` | 111 | `mingli_bench/__main__.py` |
| `28cb70252cdd8285` | 22252 | `mingli_bench/benchmark.py` |
| `2254224f9c443a66` | 7233 | `mingli_bench/cli.py` |
| `736f11a0926ca813` | 102 | `mingli_bench/data/__init__.py` |
| `bdf1f3b1379d1cce` | 17314 | `mingli_bench/data/loader.py` |
| `e4a1c615fd666737` | 3385 | `mingli_bench/data/schema.py` |
| `4094d17e67471ae3` | 358 | `mingli_bench/models/__init__.py` |
| `c037f1e1225df85c` | 3474 | `mingli_bench/models/anthropic_client.py` |
| `5a48ebbf6f48ef5e` | 4779 | `mingli_bench/models/base.py` |
| `cdf922557cf27da3` | 2864 | `mingli_bench/models/deepseek_client.py` |
| `5490dc6dd8294b0a` | 4224 | `mingli_bench/models/doubao_client.py` |
| `325f9e7b913e84ce` | 7308 | `mingli_bench/models/factory.py` |
| `28ea21faee64ea38` | 3782 | `mingli_bench/models/google_client.py` |
| `cad39f37bacf2e3c` | 3319 | `mingli_bench/models/openai_client.py` |
| `983100d90aaaeeb7` | 349 | `mingli_bench/utils/__init__.py` |
| `28fa0a935ab3b839` | 3303 | `mingli_bench/utils/config.py` |
| `6c1ea58cf0c020cb` | 1451 | `mingli_bench/utils/decorators.py` |
| `5537399bb0cdd593` | 1087 | `mingli_bench/utils/logger.py` |
| `454409f8855ee6c3` | 1829 | `mingli_bench/utils/path_utils.py` |
| `51fca6898a705ab7` | 129 | `requirements.txt` |
