# Daily research token-efficiency refactor V0.1A

Phase A introduces only a local DailyBar boundary and stable MA primitives. It
does not rewire the active V0.1 visual proof, change BUY-A/B/C, or alter the
shared renderer. The new boundary reads existing ka10081 RAW/ADJUSTED artifacts
directly through the R5 model/parser contract; it never calls a frozen proof
loader or an API.

`daily_ma/data.py` owns canonical local loading, ordering, duplicate rejection,
and inclusive slicing. `daily_ma/core.py` owns only SMA, MA percentage change,
below-MA runs, and +/-2% crossing primitives. Provisional inflection and all
BUY signal state, overlap, and restriction logic remain in V0.1.

Phase B is deferred until visual review freezes the BUY contract. It may move a
future run—not the frozen V0.1 baseline—to this boundary. Multiple signal tags
per bar remain an architecture note, not a V0.1A implementation.

The Phase-A 11-stock regression compares old and new loader date sequence,
row count, raw OHLCV, adjusted OHLCV, and ordering exactly. It also compares
MA5/10/20/60/120, MA10 slope10, below-MA10/20 runs, MA10/20 breakout, and MA10
breakdown across every loaded bar. The projected next-run dependency excludes
both frozen loader proofs; it is approximately seven source files before a
chart is needed, or eight with the existing shared renderer.
