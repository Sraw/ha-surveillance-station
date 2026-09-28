"""The Frigate review queue on its own: one message per review, bounded, and what each one keeps."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from custom_components.surveillance_station.frigate_queue import Queued, ReviewQueue, compact

from .test_frigate import T, review

FULL_AT = "custom_components.surveillance_station.frigate_queue.FRIGATE_QUEUE_MAX"


@pytest.fixture(autouse=True)
def clock():
    with patch("custom_components.surveillance_station.frigate_queue.time.time", return_value=T + 2) as now:
        yield now


def queue() -> ReviewQueue:
    """News: a message with a person in it."""
    return ReviewQueue(lambda message: "person" in message["after"]["data"]["objects"])


def put(q: ReviewQueue, rid: str, kind: str = "new", at: float = T, **changes) -> Queued:
    item = Queued(review(kind, rid=rid, **changes), received_at=at)
    q.put(rid, item)
    return item


def fail(q: ReviewQueue) -> str:
    """The next message fails with SS unreachable (so its bookmark may exist)."""
    key, item = q.next()
    q.defer(key, replace(item, maybe_made=True))
    q.done(key)
    return key


def handled(q: ReviewQueue) -> list[str]:
    """Every review waiting to be handled now, in the order it would be."""
    out = []
    while (found := q.next()) is not None:
        out.append(found[0])
        q.done(found[0])
    return out


def test_one_message_per_review_in_order() -> None:
    q = queue()
    for rid in ("a", "b", "a", "c"):
        put(q, rid, "update")
    assert (q.queued, q.coalesced) == (3, 1)
    assert handled(q) == ["a", "b", "c"]  # "a" kept its place


def test_a_later_message_replaces_the_earlier_wherever_it_waits() -> None:
    """Out of the deferred (or the replay) to the end of the fresh, knowing it was new and that its
    bookmark may exist; and a review in one queue only."""
    q = queue()
    put(q, "a")
    put(q, "b")
    assert fail(q) == "a"
    assert (q.queued, q.deferred) == (1, 1)
    later = put(q, "a", "end", at=T + 10, end=T + 9)
    assert (q.queued, q.deferred, q.coalesced) == (2, 0, 1)
    assert later.seen_new and later.maybe_made
    assert fail(q) == "b" and fail(q) == "a"
    assert q.release() and q.replaying
    put(q, "b", "end", end=T + 9)
    assert (q.queued, q.deferred, q.replaying) == (2, 0, True)
    assert handled(q) == ["b", "a"]  # fresh first


def test_news_keeps_when_it_came() -> None:
    """A message replacing one that had news (it waited for SS) is as old as that one; one that
    replaces a message without news is the news itself."""
    q = queue()
    put(q, "a", "update")
    assert put(q, "a", "update", at=T + 300).received_at == T
    put(q, "b", "update", objects=("bicycle",))
    assert put(q, "b", "update", at=T + 300).received_at == T + 300


def test_bounded() -> None:
    """Full: the oldest fresh one waits with those SS failed (not the one tried to see whether SS
    is back); and what waits for SS is bounded too, the oldest given up on."""
    q = queue()
    with patch(FULL_AT, 2):
        put(q, "k")
        fail(q)
        put(q, "a")
        assert q.canary()  # "k" again, first in line
        assert q.put("b", Queued(review("new", rid="b"), received_at=T))  # full
        assert (q.queued, q.deferred, q.dropped) == (2, 1, 0)
        assert handled(q) == ["k", "b"]  # "a" waits for SS
        for rid in ("d", "e"):
            put(q, rid)
            fail(q)
    assert (q.deferred, q.dropped) == (2, 1)
    assert list(q.waiting()) == ["d", "e"]


def test_the_canary_is_first_in_line_until_tried() -> None:
    q = queue()
    assert not q.canary()  # nothing waits
    put(q, "k")
    fail(q)
    put(q, "a")
    q.canary()
    key, _ = q.next()
    assert key == "k"
    q.done(key)
    with patch(FULL_AT, 2):
        put(q, "b")
        put(q, "c")  # full: the oldest is "a", the canary tried already
    assert handled(q) == ["b", "c"] and list(q.waiting()) == ["a"]


@pytest.mark.parametrize("where", ["fresh", "deferred", "replayed"])
def test_defer_leaves_a_newer_message_to_go_instead(where: str) -> None:
    """The message in flight fails while a newer one of its review waits (fresh, pushed out to the
    deferred, or replayed): nothing more is kept, and the newer one knows the bookmark may exist."""
    q = queue()
    with patch(FULL_AT, 1):
        put(q, "r")
        key, item = q.next()
        put(q, "r", "end", end=T + 9)
        if where != "fresh":
            put(q, "x")  # full: r's end waits with the deferred
        if where == "replayed":
            q.release()
        q.defer(key, item)
        assert q.deferred == (1 if where == "deferred" else 0)
        q.release()
        waiting = dict(iter(q.next, None))
    assert set(waiting) == ({"r"} if where == "fresh" else {"r", "x"})
    assert waiting["r"].message["type"] == "end" and waiting["r"].maybe_made
    assert "x" not in waiting or not waiting["x"].maybe_made


def test_failed_again_mid_replay() -> None:
    q = queue()
    for rid in ("a", "b"):
        put(q, rid)
        fail(q)
    put(q, "c")
    assert q.release() and not q.release()
    assert q.next()[0] == "c"  # fresh first
    q.done("c")
    assert q.hold_replay() and not q.hold_replay()
    assert (q.queued, q.deferred) == (0, 2)


def test_the_newer_message_of_a_retry() -> None:
    q = queue()
    put(q, "r")
    key, _ = q.next()
    assert q.newer(key) is None
    put(q, "r", "end", end=T + 9)
    newer = q.newer(key)
    assert newer.message["type"] == "end" and q.queued == 0
    q.retrying(key, replace(newer, maybe_made=True))
    q.retrying("other", newer)  # not the one being handled: nothing
    q.stop()
    kept = q.waiting()["r"]
    assert (kept.message["type"], kept.maybe_made) == ("end", True)


def test_waiting_leaves_the_queues_as_they_are() -> None:
    """For next time: what waits for SS, the replay, the one in flight (unless a newer message of
    its review waits: that one then, knowing the bookmark may exist), the fresh; all dated."""
    q = queue()
    put(q, "d")
    fail(q)
    q.release()
    put(q, "p")
    fail(q)  # "p" waits for SS, "d" is replayed
    put(q, "c")
    q.next()
    put(q, "n")
    put(q, "c", "update")
    waiting = q.waiting()
    assert list(waiting) == ["p", "d", "n", "c"]
    assert waiting["c"].maybe_made and waiting["c"].message["type"] == "update" and not waiting["n"].maybe_made
    assert all(item.failed_at == T + 2 for item in waiting.values())
    assert waiting["n"].message == compact(review("new", rid="n"))  # only what handle() reads
    assert (q.queued, q.deferred) == (3, 1)
    live = dict(iter(q.next, None))
    assert list(live) == ["n", "c", "d"] and not live["c"].maybe_made and live["c"].failed_at is None

    alone = queue()
    put(alone, "c")
    alone.next()
    alone.stop()  # unloaded mid-request
    assert (alone.queued, alone.deferred) == (0, 1) and alone.waiting()["c"].maybe_made


def test_given_up_after_a_day_from_the_first_failure(clock) -> None:
    q = queue()
    put(q, "a")
    fail(q)
    clock.return_value = T + 50_000
    assert q.canary()
    fail(q)  # failed again: still dated from the first time
    assert q.expire() == 0
    clock.return_value = T + 2 + 86_401
    assert q.expire() == 1 and (q.deferred, q.dropped) == (0, 1)


def test_kept_twice_keeps_what_either_told() -> None:
    """Defensive (a review is in one queue at a time): kept again, it keeps the first failure's
    date and what the earlier one told."""
    q = queue()
    q._keep("r", Queued(review("new", rid="r"), maybe_made=True, seen_new=True, failed_at=T - 50))
    q._keep("r", Queued(review("end", rid="r", end=T + 9)))
    kept = q.waiting()["r"]
    assert (kept.message["type"], kept.maybe_made, kept.seen_new, kept.failed_at) == ("end", True, True, T - 50)


def test_stored_as_0_18_stored_it() -> None:
    """Frigate's message with the flags beside it: what 0.18 wrote loads, and is written back the same way."""
    q = queue()
    put(q, "a")
    fail(q)
    stored = q.stored()
    assert stored == [["a", {"_maybe_made": True, "_failed_at": T + 2, "_received_at": T, **compact(review("new", rid="a"))}]]
    again = queue()
    again.load(stored, T + 50)
    assert again.stored() == stored

    old = queue()
    old.load([
        ["b", {"_seen_new": True, "_received_at": T, **review("update", rid="b")}],
        ["c", review("update", rid="c")],  # before 0.18: undated
        ["x", "junk"],
        ["y", {"type": "new"}],
    ], T + 50)
    assert old.stored() == [
        ["b", {"_seen_new": True, "_maybe_made": True, "_failed_at": T + 50, "_received_at": T, **review("update", rid="b")}],
        ["c", {"_maybe_made": True, "_failed_at": T + 50, **review("update", rid="c")}],
    ]
    for unreadable in ([["a"]], [["a", {**review("new"), "_failed_at": "x"}]]):
        with pytest.raises((ValueError, TypeError)):
            queue().load(unreadable, T)
