# Third-Party Components

## Overview

Open Agent Memory source code is Apache-2.0. Python packages, system packages, model endpoints, model weights, evaluation datasets, and generated artifacts retain their own licenses and terms; inclusion in a lockfile or example does not relicense them.

## Runtime Dependencies

The authoritative Python dependency names and versions are in `pyproject.toml` and `uv.lock`. Before a public release, generate and review an SBOM, preserve required notices, and verify every bundled parser/static asset permits redistribution.

## Models and Data

No model, credential, private report, company trace, or benchmark dataset is bundled by default. Operators are responsible for approving configured model endpoints and their data-handling terms. Public conformance fixtures use invented data only.
