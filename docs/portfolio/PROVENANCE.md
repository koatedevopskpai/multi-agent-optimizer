# Evidence provenance

This repository contains a curated engineering reference using synthetic projects, costs and permissions. Source code, tests, dated results and selected redacted screenshots support its claims. Detailed original development records are preserved separately; this public snapshot does not claim to expose every raw operational artifact.

The [release manifest](release-manifest.json) records SHA256 values for published file bytes. [Approved images](approved-images.json) records the exact screenshot derivatives accepted by individual visual privacy review. Image masks remove private details without altering visible results. Screenshots show historical states and must be read with their dates and documented limitations.

New documentation summaries do not rewrite original experiment records. A new derivative has its own hash; unchanged images retain theirs. Text is checked out with LF line endings, while images are binary. Verify the published snapshot using `python scripts/verify_portfolio_manifest.py`.

AI coding and review tools assisted implementation, testing and evidence preparation. Koate Kpai owns the engineering decisions. This synthetic reference is distinct from client production experience.
