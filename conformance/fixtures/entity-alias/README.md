# Entity and alias scenarios

## Overview

`cases.json` contains nine synthetic retain-to-recall scenarios: three Chinese and six English. They cover numeric/unit wording (`10 楼` and `十层`), declared abbreviations, bilingual names, former names, person initials, shared abbreviations for distinct organizations, places and explicit product/project causes. The inputs contain no external dataset or provider dependency.

Each case contains separate source `sessions`, a question/date, a reference answer and evaluation-only support markers. Retain only each session's `messages`; never ingest the question, reference answer, purpose or support markers. Use a fresh user for each case, preserve session order, and wait for direct indexing plus semantic/temporal readiness before recall. Source text and facts remain independently supported; matching names alone must not merge identities.

Evaluate fast with `fast_graph:false`, fast with `fast_graph:true`, and mid with its ordinary graph. Use identical indexed history, complete question, context budget and reference clock for each comparison. Save exact cited contexts, answer correctness, identity support, generation-only indexing usage and indexing duration. Repeat complete HTTP recall passes in alternating fast order and retain every timing sample, including cold calls. Correct answers alone do not prove an alias graph path was used; inspect stored bindings and selected path witnesses separately. Whole-text retrieval may already cover a naming variant, and semantic entity-name indexing is not required by these fixtures.
