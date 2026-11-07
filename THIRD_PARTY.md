# Third-Party Components

## Overview

OAM-authored source code is Apache-2.0; adapted source portions retain their listed license. Python packages, system packages, model endpoints, model weights, evaluation datasets, and generated artifacts retain their own licenses and terms; inclusion in a lockfile or example does not relicense them.

## Runtime Dependencies

The authoritative Python dependency names and versions are in `pyproject.toml` and `uv.lock`. Release distributions include this notice and the companion license texts in `third_party_licenses/`. Preserve the installed packages' `.dist-info/licenses` directories in container images and review the exact image's SBOM before publication.

The locked runtime graph includes MIT, BSD, Apache-2.0, MPL-2.0, LGPL-3.0-only, PSF-2.0 and other package-specific terms. In particular:

| Component | License and notice location |
|---|---|
| Extraction overflow splitting and retry | MIT; Copyright (c) 2025 Vectorize AI, Inc. The adapted source notice is included as `third_party_licenses/extraction-split-MIT.txt`. |
| `jieba` | MIT; its upstream license is included as `third_party_licenses/jieba-MIT.txt` because the installed package has no license file. |
| `psycopg`, `psycopg-pool` | LGPL-3.0-only; installed package license files contain the LGPL text, and `third_party_licenses/GPL-3.0.txt` provides the GPLv3 companion text. The release runtime uses the system `libpq`; review that system package and its license in the release image. `psycopg-binary` is a local development dependency and is excluded from the release runtime. Direct recipients to the [corresponding Psycopg source](https://github.com/psycopg/psycopg). |
| `certifi` | MPL-2.0; retain its installed license and direct recipients to the [source](https://github.com/certifi/python-certifi). |
| `pypdfium2` and bundled PDFium | The installed wheel carries its own license texts and the PDFium build's dependency notices under `.dist-info/licenses/`; preserve all of them. |
| Synthetic medical PDFs with embedded Noto Sans SC subsets | SIL Open Font License 1.1; the exact upstream notice is in `conformance/fixtures/medical/bilingual/OFL.txt`. The full font file is not distributed. |

## Models and Data

OAM bundles two compressed official Qwen3-Embedding tokenizer assets from the Qwen Team under Apache-2.0, with the license in `third_party_licenses/qwen-tokenizer-Apache-2.0.txt`. These contain tokenizer vocabulary and processing configuration, not model weights. The distributed gzip files preserve the exact upstream JSON bytes; SHA-256 below identifies decompressed content. Both use the upstream EOS postprocessor with special tokens enabled; saved truncation and padding are disabled at load time.

| Asset | Pinned upstream source | Decompressed SHA-256 |
|---|---|---|
| `qwen3-embedding-0.6b.json.gz` | [Qwen3-Embedding-0.6B tokenizer.json](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/blob/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3/tokenizer.json) | `def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a` |
| `qwen3-embedding-4b-8b.json.gz` | [Qwen3-Embedding-4B tokenizer.json](https://huggingface.co/Qwen/Qwen3-Embedding-4B/blob/5cf2132abc99cad020ac570b19d031efec650f2b/tokenizer.json); identical bytes in [8B](https://huggingface.co/Qwen/Qwen3-Embedding-8B/blob/1d8ad4ca9b3dd8059ad90a75d4983776a23d44af/tokenizer.json) | `83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d` |

The 0.6B vocabulary additionally registers `<tool_response>`, `</tool_response>`, `<think>` and `</think>` as ordinary added tokens; the 4B/8B asset omits them. No model weights, credential, private report, company trace, or benchmark dataset is bundled by default. Operators are responsible for approving configured model endpoints and their data-handling terms. Public conformance fixtures use invented data only.
