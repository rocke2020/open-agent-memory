# Third-Party Components

## Overview

Open Agent Memory source code is Apache-2.0. Python packages, system packages, model endpoints, model weights, evaluation datasets, and generated artifacts retain their own licenses and terms; inclusion in a lockfile or example does not relicense them.

## Runtime Dependencies

The authoritative Python dependency names and versions are in `pyproject.toml` and `uv.lock`. Release distributions include this notice and the companion license texts in `third_party_licenses/`. Preserve the installed packages' `.dist-info/licenses` directories in container images and review the exact image's SBOM before publication.

The locked runtime graph includes MIT, BSD, Apache-2.0, MPL-2.0, LGPL-3.0-only, PSF-2.0 and other package-specific terms. In particular:

| Component | License and notice location |
|---|---|
| `jieba` | MIT; its upstream license is included as `third_party_licenses/jieba-MIT.txt` because the installed package has no license file. |
| `psycopg`, `psycopg-pool` | LGPL-3.0-only; installed package license files contain the LGPL text, and `third_party_licenses/GPL-3.0.txt` provides the GPLv3 companion text. The release runtime uses the system `libpq`; review that system package and its license in the release image. `psycopg-binary` is a local development dependency and is excluded from the release runtime. Direct recipients to the [corresponding Psycopg source](https://github.com/psycopg/psycopg). |
| `certifi` | MPL-2.0; retain its installed license and direct recipients to the [source](https://github.com/certifi/python-certifi). |
| `pypdfium2` and bundled PDFium | The installed wheel carries its own license texts and the PDFium build's dependency notices under `.dist-info/licenses/`; preserve all of them. |
| Synthetic medical PDFs with embedded Noto Sans SC subsets | SIL Open Font License 1.1; the exact upstream notice is in `conformance/fixtures/medical/bilingual/OFL.txt`. The full font file is not distributed. |

## Models and Data

No model, credential, private report, company trace, or benchmark dataset is bundled by default. Operators are responsible for approving configured model endpoints and their data-handling terms. Public conformance fixtures use invented data only.
