# Search, Recall, and Feedback

Mnemos separates retrieval from memory usage state.

## Search

`mnemos search` is read-only by default. It returns matching memory items without
changing memory content, frontmatter, `access_count`, stage, layer, timestamps,
or promotion state.

Use `mnemos search --touch` only for legacy integrations that still need the old
`access_count` update. This mode updates `access_count` as
`legacy_access_count`-style compatibility data, but it does not run
search-based auto-promotion.

During the deprecation period, operators can set:

```bash
MNEMOS_SEARCH_TOUCH_DEFAULT=1
```

That flag makes `mnemos search` behave like `mnemos search --touch` for old
callers. New integrations should not use it.

## Recall

`mnemos recall --json --request-file ...` is the stable provider retrieval
contract. Recall is always read-only and does not update access counters or
trigger promotion.

Recall ranking uses retrieval relevance plus validated Feedback usage. It does
not treat search hits, read counts, `access_count`, or `legacy_access_count` as
validated usage.

## Feedback

`mnemos feedback --json --request-file ...` records actual use events in the
append-only Feedback ledger and updates the Usage Projection.

Only `applied` and `validated` feedback can evaluate promotion. `retrieved`,
`selected`, and `accepted` are retrieval or context-delivery signals; they do
not promote memory.
