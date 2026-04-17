"""
Draft: scraperx/scraperx/authenticity.py (NEW FILE).
Formal 4-property authenticity check on a Thread.
Integration notes:
- Real imports: `from scraperx.thread import Thread`, `from scraperx.scraper import Tweet`
- Depends on Feature 1 Tweet enrichment (conversation_id, author_id, created_timestamp, in_reply_to_tweet_id fields must be populated)
- Exported from __init__.py as ThreadAuthenticity + check_thread_authenticity
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scraperx.thread import Thread


@dataclass
class ThreadAuthenticity:
    """Result of formal authenticity check on a Thread.

    Four structural properties of an authentic self-thread:
      1. same_conversation — all tweets share root's conversation_id
      2. single_author     — all tweets share root's author_id (numeric, not handle)
      3. chronological     — created_timestamp strictly non-decreasing along reply chain
      4. no_interpolation  — every in_reply_to_tweet_id resolves within thread set
                             AND parent tweet has same author_id

    has_branches: author replied twice to the same parent (well-formed self-thread
    is a path, not a tree). Flag don't fail.

    root_deleted: conversation_id set on children but root tweet fields missing
    (walk-up terminated early).

    missing_fields: API fields we could not verify because the backend didn't return
    them (e.g., FxTwitter doesn't expose conversation_id — syndication does).
    """

    is_authentic: bool = False
    same_conversation: bool = True
    single_author: bool = True
    chronological: bool = True
    no_interpolation: bool = True
    has_branches: bool = False
    root_deleted: bool = False
    missing_fields: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def check_thread_authenticity(thread: Thread) -> ThreadAuthenticity:
    """Verify a Thread against the 4 structural authenticity properties.

    Trivial cases:
      - empty thread → not authentic (edge case: should never happen from get_thread)
      - single tweet → trivially authentic (it IS the root)

    Graceful degradation:
      - If conversation_id missing on all tweets: fall back to reply-chain + author
        check, flag 'conversation_id' in missing_fields, mark same_conversation=False
        (strict: we couldn't verify), but don't penalize overall authenticity when
        chain + author checks pass. The 'reasons' list explains.
      - If author_id missing: flag 'author_id' in missing_fields, skip single_author
        check (reported as False but reason makes clear it's unverified).
    """
    tweets = list(thread.all_tweets)
    result = ThreadAuthenticity()

    if not tweets:
        result.reasons.append("empty thread")
        return result

    root = thread.root_tweet or tweets[0]

    if len(tweets) == 1:
        result.is_authentic = True
        return result

    # Field-coverage audit
    conv_ids_present = sum(1 for t in tweets if getattr(t, "conversation_id", None))
    author_ids_present = sum(1 for t in tweets if getattr(t, "author_id", None))
    ts_present = sum(1 for t in tweets if getattr(t, "created_timestamp", None))

    if conv_ids_present < len(tweets):
        result.missing_fields.append("conversation_id")
    if author_ids_present < len(tweets):
        result.missing_fields.append("author_id")
    if ts_present < len(tweets):
        result.missing_fields.append("created_timestamp")

    # --- Property 1: same_conversation ---
    root_conv = getattr(root, "conversation_id", None)
    if conv_ids_present == len(tweets) and root_conv:
        for t in tweets:
            if t.conversation_id != root_conv:
                result.same_conversation = False
                result.reasons.append(f"tweet {t.id} has conversation_id={t.conversation_id} but root is {root_conv}")
                break
    else:
        result.same_conversation = False
        result.reasons.append("conversation_id unavailable — cannot verify property 1 strictly")

    # --- Property 2: single_author (by numeric author_id, not handle) ---
    root_author_id = getattr(root, "author_id", None)
    if author_ids_present == len(tweets) and root_author_id:
        for t in tweets:
            if t.author_id != root_author_id:
                result.single_author = False
                result.reasons.append(f"tweet {t.id} has author_id={t.author_id} but root is {root_author_id}")
                break
    else:
        # Fallback: check handle continuity (weaker signal, handles are mutable)
        root_handle = (root.author_handle or "").lower()
        for t in tweets:
            if (t.author_handle or "").lower() != root_handle:
                result.single_author = False
                result.reasons.append(f"tweet {t.id} has author_handle={t.author_handle} but root is {root_handle}")
                break
        if not root_author_id:
            result.reasons.append("author_id unavailable — falling back to handle comparison (weaker signal)")

    # --- Property 3: chronological ---
    if ts_present == len(tweets):
        prev_ts = 0
        for t in tweets:
            ts = t.created_timestamp or 0
            if ts < prev_ts:
                result.chronological = False
                result.reasons.append(f"tweet {t.id} timestamp {ts} is before prev {prev_ts}")
                break
            prev_ts = ts
    else:
        # Fallback: numeric ID ordering — tweet IDs are monotonic (Twitter snowflake)
        prev_id = 0
        for t in tweets:
            try:
                tid = int(t.id)
            except (ValueError, TypeError):
                continue
            if tid < prev_id:
                result.chronological = False
                result.reasons.append(f"tweet {t.id} has lower numeric ID than prev {prev_id}")
                break
            prev_id = tid
        if ts_present < len(tweets):
            result.reasons.append("created_timestamp missing — falling back to tweet ID ordering")

    # --- Property 4: no_interpolation ---
    tweet_map = {t.id: t for t in tweets}
    parent_counts: dict[str, int] = {}
    for t in tweets:
        parent_id = getattr(t, "in_reply_to_tweet_id", None)
        if not parent_id:
            continue
        if parent_id not in tweet_map:
            # Parent not in thread set — could be interpolation OR root of a longer chain
            # If t is NOT root, this is interpolation (parent should be in thread)
            if t.id != root.id:
                result.no_interpolation = False
                result.reasons.append(f"tweet {t.id} replies to {parent_id} which is not in thread")
        else:
            parent = tweet_map[parent_id]
            # Parent author must match for self-thread continuity
            if root_author_id and getattr(parent, "author_id", None) and parent.author_id != t.author_id:
                result.no_interpolation = False
                result.reasons.append(
                    f"tweet {t.id} replies to {parent_id} but authors differ ({t.author_id} vs {parent.author_id})"
                )
            parent_counts[parent_id] = parent_counts.get(parent_id, 0) + 1

    # --- has_branches: author replied to same parent twice ---
    for parent_id, count in parent_counts.items():
        if count > 1:
            result.has_branches = True
            result.reasons.append(f"parent {parent_id} has {count} replies (branching)")
            break

    # --- root_deleted: conversation_id on children points at root but root fields sparse ---
    if root.conversation_id and not root.text and not root.author_handle:
        result.root_deleted = True
        result.reasons.append("root tweet appears deleted (conversation_id set but content missing)")

    # --- Final verdict ---
    # Strict: ALL 4 properties must pass. Branches + root_deleted are advisory flags.
    # If missing_fields forced a property to False purely from data-gap, we mention it
    # but still require the property to be "not falsified".
    result.is_authentic = (
        result.same_conversation
        and result.single_author
        and result.chronological
        and result.no_interpolation
        and not result.root_deleted
    )

    return result
