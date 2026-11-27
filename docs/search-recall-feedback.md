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

프로젝트 ID와 체크아웃 루트 해시가 있고 기본 검색이 후보 한도를 채우지 못하면,
한국어 요청의 짧은 검색어를 FTS로 대조해 남은 칸에 같은 범위의 활성 원본 연결 기록을
추가할 수 있습니다. 기본 후보의 순서와 선택 결과를 보존합니다.
보완 검색은 색인에서 다른 프로젝트나 체크아웃으로 확인된 항목을 순위 제한 전에
제외하고, 범위가 일치하는 항목을 범위 정보가 없는 이전 항목보다 먼저 읽습니다.
최종 후보의 범위와 사용 가능 상태는 저장된 기록으로 확인합니다.
이 후보는 `results`에만 포함되며 `selected_ids`에는 추가되지 않습니다.
확장으로 찾은 후보 수는 `diagnostics.korean_candidate_count`에 기록됩니다.
보완 후보의 FTS 점수가 같으면 `validated` 피드백으로 후보 순서를 정합니다.
`source_reviewed_candidate`의 `applied` 피드백은 적용 건수에 남습니다. 이 후보의
사용 이력에 따른 순위 가점은 `validated` 피드백을 받은 뒤 반영됩니다.
`source_reviewed_candidate`의 검증 피드백에 요청과 원본을 묶은 해시가 있으면, 같은 원문 요청과
원본 위치·내용을 가진 후보의 점수 구성에 이 일치를 표시합니다.
`evidence-sha256:` 태그가 있는 경우 이 해시에 근거 파일의 지문도 포함합니다.

## Feedback

`mnemos feedback --json --request-file ...` records actual use events in the
append-only Feedback ledger and updates the Usage Projection.

`mnemos read --read-only --json <id>`는 접근 횟수를 갱신하지 않고 현재
`eligible_for_use`를 반환한다. 폐기·대체된 기억에는 새 `applied` 또는
`validated` 피드백을 기록하지 않는다. 이미 수락한 동일 이벤트의 재전송은
기존 성공 기록을 반환한다.
여러 ID는 `mnemos read --read-only --json --eligibility-only <id> ...`로
한 번에 확인할 수 있다. 요청 순서로 ID와 상태를 반환하며 확인되지 않은 ID의
`eligible_for_use`는 `null`이다.

Only `applied` and `validated` feedback can evaluate promotion. `retrieved`,
`selected`, and `accepted` are retrieval or context-delivery signals; they do
not promote memory.
