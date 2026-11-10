# Sorted candidates — run-toy0001 (toy/cache)

**Objective:** reduce median TTFT

## Ranking summary

| # | Candidate | Module | Symbol | Impact | Score | Rationale |
|---|---|---|---|---|---|---|
| 1 | [`cand-toy_cache-0002`](../modules/toy_cache/x__cand-toy_cache-0002.md) | toy/cache | LRUPolicy.evict | medium | 91 | Eviction dominates the multi-turn hit rate |
| 2 | [`cand-toy_cache-0001`](../modules/toy_cache/x__cand-toy_cache-0001.md) | toy/cache | cache constants (BLOCK_SIZE, MAX_BLOCKS) | high | 77 | Block size is coarse |
| 3 | [`cand-toy_cache-0003`](../modules/toy_cache/x__cand-toy_cache-0003.md) | toy/cache | LRUPolicy.touch | low | 40 | small |
| 4 | [`cand-toy_cache-0004`](../modules/toy_cache/x__cand-toy_cache-0004.md) | toy/cache | gone | low | — | not ranked by judge; pre-score order |
