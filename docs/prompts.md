# OAM runtime prompts

## Overview

OAM keeps its authored model instructions in the packaged UTF-8 text files linked below. The runtime reads these files when their modules are imported. Review and edit the text files directly, then restart the relevant processes to load changes. Request-specific source text, references and custom Profile instructions are assembled separately as input data.

## 1. Memory and Profile generation

The extraction system message combines the shared rules with the grouped prefix and reference suffix selected by the call path. Insight and Profile generation each use their own system prompt.

| Prompt file | Purpose | Runtime consumer |
|---|---|---|
| [extraction-prompt.txt](../src/oam/indexing/extraction-prompt.txt) | Shared fact extraction rules and output contract | [extraction.py](../src/oam/indexing/extraction.py) |
| [extraction-group-prefix.txt](../src/oam/indexing/extraction-group-prefix.txt) | Passage ownership and output ordering for grouped extraction | [grouping.py](../src/oam/indexing/grouping.py) |
| [extraction-reference-suffix.txt](../src/oam/indexing/extraction-reference-suffix.txt) | Existing-fact reference rules | [extraction.py](../src/oam/indexing/extraction.py) |
| [insight-prompt.txt](../src/oam/indexing/insight-prompt.txt) | Supported Insight synthesis and subtype selection | [extraction.py](../src/oam/indexing/extraction.py) |
| [profiles/prompt.txt](../src/oam/profiles/prompt.txt) | Default Profile generation and custom-format rules | [prompt.py](../src/oam/profiles/prompt.py), [worker.py](../src/oam/profiles/worker.py) |

Extraction and Insight files retain their exact whitespace during loading. The Profile loader strips final newline characters. Intentional indexing prompt changes require an indexing-recipe update; Profile prompt text participates in refresh identity.

## 2. Resource reading

The [source indexing worker](../src/oam/workers/index_source.py) loads these prompts for document abstracts and visual extraction. Loaders strip final newline characters.

| Prompt file | Purpose |
|---|---|
| [abstract-prompt.txt](../src/oam/resources/abstract-prompt.txt) | Summarize cited document passages |
| [pdf-vision-system-prompt.txt](../src/oam/resources/pdf-vision-system-prompt.txt) | Read detailed text and tables from a physical PDF page |
| [pdf-vision-user-prompt.txt](../src/oam/resources/pdf-vision-user-prompt.txt) | Select the physical page using the `{page_number}` placeholder |
| [image-vision-system-prompt.txt](../src/oam/resources/image-vision-system-prompt.txt) | Read image details and optional regions |
| [image-vision-user-prompt.txt](../src/oam/resources/image-vision-user-prompt.txt) | Request the image extraction result |

## 3. Query planning and deployment probes

Optional delegated recall uses the planner template. Deployment preflight uses the fixed probe instructions and rerank query. Loaders strip final newline characters.

| Prompt file | Purpose | Runtime consumer |
|---|---|---|
| [planner-prompt.txt](../src/oam/api/planner-prompt.txt) | Generate retrieval queries; `{max_queries}` comes from search configuration | [search.py](../src/oam/api/search.py) |
| [preflight-chat-prompt.txt](../src/oam/models/preflight-chat-prompt.txt) | Check visible JSON chat output | [client.py](../src/oam/models/client.py) |
| [preflight-vision-prompt.txt](../src/oam/models/preflight-vision-prompt.txt) | Check visual recognition of the probe image | [client.py](../src/oam/models/client.py) |
| [preflight-rerank-query.txt](../src/oam/models/preflight-rerank-query.txt) | Check rerank scores against fixed probe documents | [client.py](../src/oam/models/client.py) |
