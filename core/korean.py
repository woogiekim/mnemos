"""Korean query preprocessing — particle stripping and alias expansion.

This module normalises Korean search queries before they reach the FTS5
engine, reducing the zero-result rate for Korean-language users.

Two transformations are applied, in order:

1. **Alias expansion**: transliterations and abbreviations (e.g. ``에이전트크루``)
   are mapped to their indexed English equivalents (e.g. ``agent-crew``).
   The built-in alias table covers the most common mnemos-specific terms;
   callers can extend it by passing a custom ``aliases`` dict.

2. **Particle stripping**: trailing Korean grammatical particles (조사) are
   stripped from each token.  The regex matches the *longest* particle first
   so that multi-character particles (``에서``, ``으로``, ``부터``, ``까지``)
   are correctly removed before their shorter sub-sequences.

Usage
-----
>>> from core.korean import preprocess_query
>>> preprocess_query("에이전트크루의 캡처율에서")
'agent-crew capture'
>>> preprocess_query("니모스를 검색하고")
'mnemos 검색하고'
"""
from __future__ import annotations

import re
from typing import Mapping

# ---------------------------------------------------------------------------
# Built-in alias table
# ---------------------------------------------------------------------------
# Maps Korean transliterations / abbreviations to their indexed English forms.
# Keys are matched case-insensitively (after NFKC normalisation).
# Values replace the entire token — no partial substitution.
#
# To extend this table at runtime, pass ``extra_aliases`` to ``preprocess_query``
# or call ``build_alias_re()`` with a merged dict.

DEFAULT_ALIASES: dict[str, str] = {
    # mnemos product terms
    "에이전트크루": "agent-crew",
    "니모스": "mnemos",
    "캡처": "capture",
    "훅": "hook",
    "훅스": "hooks",
    "레이어": "layer",
    "레이어스": "layers",
    "세션": "session",
    "글로벌": "global",
    "프로젝트": "project",
    "검색": "search",
    "프로모션": "promotion",
    "프로모트": "promote",
    "이페머럴": "ephemeral",
    "워킹": "working",
    "캡처율": "capture",
    # common tech terms that appear in indexed content
    "에이전트": "agent",
    "파이프라인": "pipeline",
    "백엔드": "backend",
    "프론트엔드": "frontend",
    "데이터베이스": "database",
    "인덱스": "index",
    "쿼리": "query",
    "커밋": "commit",
    "브랜치": "branch",
    "리뷰": "review",
    "스크립트": "script",
    "테스트": "test",
    "픽스": "fix",
    "피처": "feature",
    "버그": "bug",
}

# ---------------------------------------------------------------------------
# Particle suffix regex
# ---------------------------------------------------------------------------
# Lists particles longest-first so the regex engine tries to match
# longer suffixes before shorter ones. This prevents ``에서`` being matched
# as ``에`` + ``서`` (two separate particles).
#
# Reference: https://en.wikipedia.org/wiki/Korean_postpositions
_PARTICLES_LONGEST_FIRST = [
    # 4-char
    "에서는",
    "으로는",
    "에게서",
    "한테서",
    "로부터",
    "에서도",
    # 3-char
    "에서",
    "으로",
    "부터",
    "까지",
    "에게",
    "한테",
    "처럼",
    "보다",
    "마다",
    "라도",
    "이나",
    "이랑",
    "이며",
    # 2-char
    "로서",
    "로써",
    "에도",
    "에만",
    "에는",
    "에서",
    "의해",
    "이라",
    "이든",
    "이면",
    "이야",
    "이요",
    "으로",
    "과의",
    "와의",
    # 1-char
    "을",
    "를",
    "의",
    "은",
    "는",
    "이",
    "가",
    "와",
    "과",
    "도",
    "만",
    "로",
    "에",
]

# Build the compiled pattern once at module load.
# The alternation is ordered longest-to-shortest so the regex engine
# uses the greedy leftmost match, which here always picks the longest
# matching suffix.
_PARTICLE_RE = re.compile(
    "(" + "|".join(re.escape(p) for p in _PARTICLES_LONGEST_FIRST) + ")$"
)


def _has_korean(text: str) -> bool:
    """Return True if *text* contains at least one Hangul syllable block."""
    return bool(re.search(r"[가-힣]", text))


def strip_particles(token: str) -> str:
    """Remove a trailing Korean grammatical particle from *token*.

    Only the *longest* matching particle suffix is removed (at most one pass).
    If the result would be empty (the entire token was a particle), the
    original token is returned unchanged.

    Examples
    --------
    >>> strip_particles("에이전트크루의")
    '에이전트크루'
    >>> strip_particles("훅에서")
    '훅'
    >>> strip_particles("검색")
    '검색'
    >>> strip_particles("agent-crew")  # no Korean → unchanged
    'agent-crew'
    """
    if not _has_korean(token):
        return token
    stripped = _PARTICLE_RE.sub("", token)
    # Guard: if the whole token was consumed, return original
    return stripped if stripped else token


def expand_aliases(
    token: str,
    aliases: Mapping[str, str] | None = None,
) -> str:
    """Replace *token* with its English alias if one exists.

    Matching is case-insensitive and uses NFKC-normalised forms.
    When *aliases* is ``None``, :data:`DEFAULT_ALIASES` is used.

    Returns the alias value when a match is found, otherwise *token*
    unchanged.

    Examples
    --------
    >>> expand_aliases("에이전트크루")
    'agent-crew'
    >>> expand_aliases("니모스")
    'mnemos'
    >>> expand_aliases("agent-crew")  # no alias → unchanged
    'agent-crew'
    """
    import unicodedata

    table = aliases if aliases is not None else DEFAULT_ALIASES
    normalised = unicodedata.normalize("NFKC", token).lower()
    return table.get(normalised, token)


def preprocess_query(
    query: str,
    aliases: Mapping[str, str] | None = None,
) -> str:
    """Normalise a search query for FTS5 by expanding aliases and stripping particles.

    The transformation pipeline per token:
    1. Expand alias (``에이전트크루`` → ``agent-crew``).
       When an alias fires, particle stripping is skipped for that token
       (the alias value is already a clean English term).
    2. Strip trailing Korean particle (``니모스를`` → ``니모스``).

    Non-Korean tokens are passed through unchanged.

    Empty or whitespace-only queries are returned as-is.

    Parameters
    ----------
    query:
        Raw query string from the user (may be Korean, English, or mixed).
    aliases:
        Custom alias table.  When ``None``, :data:`DEFAULT_ALIASES` is used.
        Pass an empty dict ``{}`` to disable alias expansion entirely.

    Returns
    -------
    str
        The preprocessed query string with tokens joined by single spaces.

    Examples
    --------
    >>> preprocess_query("에이전트크루의 캡처율에서")
    'agent-crew capture'
    >>> preprocess_query("니모스를 검색하고")
    'mnemos 검색하고'
    >>> preprocess_query("agent-crew search")  # English unchanged
    'agent-crew search'
    >>> preprocess_query("")
    ''
    """
    if not query or not query.strip():
        return query

    tokens = query.split()
    processed: list[str] = []

    for token in tokens:
        # Step 1: alias expansion (full-token match after particle strip)
        # Try alias on the raw token first, then on particle-stripped form.
        alias_result = expand_aliases(token, aliases=aliases)
        if alias_result != token:
            # Alias matched the raw form — use it directly.
            processed.append(alias_result)
            continue

        # Step 2: strip particle
        stripped = strip_particles(token)

        # Step 3: try alias again on the stripped form
        alias_stripped = expand_aliases(stripped, aliases=aliases)
        if alias_stripped != stripped:
            processed.append(alias_stripped)
            continue

        # No alias match — keep stripped form
        processed.append(stripped)

    return " ".join(processed)


def expand_query(
    query: str,
    aliases: Mapping[str, str] | None = None,
) -> list[str]:
    """Return all query variants that should be searched.

    For a pure-Korean query that maps entirely to English via aliases, returns
    ``[english_form, original_preprocessed]``.  For mixed or English queries,
    returns just ``[preprocessed]``.

    This allows callers to run multiple FTS passes and union the results when
    the alias expansion changes the entire query, maximising recall.

    Examples
    --------
    >>> expand_query("에이전트크루")
    ['agent-crew', '에이전트크루']
    >>> expand_query("search query")
    ['search query']
    >>> expand_query("니모스의 캡처")
    ['mnemos capture', '니모스 캡처']
    """
    preprocessed = preprocess_query(query, aliases=aliases)
    raw_stripped = preprocess_query(query, aliases={})  # particle-strip only

    variants: list[str] = []
    if preprocessed not in variants:
        variants.append(preprocessed)
    if raw_stripped and raw_stripped != preprocessed and raw_stripped not in variants:
        variants.append(raw_stripped)
    return variants
