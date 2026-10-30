# Demo walkthrough

Three bugs, each run end to end: Sparring Partner finds the bug and shrinks it, the learner
predicts, climbs the hint ladder to the level 3 trace, fixes it, and the fix is checked on
hundreds of fresh inputs.

Each `transcripts/*.md` file is a recording of one real run, with the Token Factory models
writing the properties and hints and the Token Factory sandbox running the code. Model output
varies from run to run, so wording, timings and a few details will differ. To record a new run against a running backend:

```bash
cd backend
uv run python scripts/run_demo.py merge   # or: search, lru  (--url http://localhost:8001)
```

The script plays the learner. It confirms the proposed properties, adds the demo's key
property (see below), and predicts what a correct version returns. It then asks for hints
up to level 3, submits the fix, and answers one follow-up question.

## 1. AI-written `merge_intervals` ([code](merge_intervals.py), [run](transcripts/merge.md))

**Intent:** "Merge overlapping intervals, given as [start, end] lists sorted by start."

**Where the code comes from.** We asked four Token Factory models (Nemotron Nano,
Qwen3-30B, Gemma 3 27B and others) for `merge_intervals` 36 times, with several phrasings.
When told the input is sorted, they skip sorting, which is the "assumes sorted input" half
of this bug. None of them used `<` for the overlap test: every one wrote `<=`. So the demo
file is one Qwen3-30B answer, unchanged except for that one character. Qwen's own comment
says "current starts *before* last ends", and `<` is what that comment literally says.

| What to show | What the recorded run did |
|---|---|
| `[[1, 4], [4, 5]]` is found and shrunk | First question: "What should `merge_intervals([[1, 4], [4, 5]])` return?" Hypothesis shrinks its own find to an equally small two-interval input, and the hand-picked "touching ranges" edge case wins the tie because it's easier to read. |
| Level 3 trace shows the comparison at the touching intervals | Rows show `current = [4, 5]` and `last = [1, 4]`. The marked row is where `[4, 5]` is appended instead of merged. |
| The `<=` fix passes 500 fresh inputs | Passed, with 3,516 inputs checked: 500 fresh inputs per property, plus the edge cases and nearby variants. |
| The follow-up breaks it again | Follow-up: `merge_intervals([[5, 6], [1, 3], [2, 4]])`. The fixed code returns `[[5, 6]]` and loses two intervals. |

**Notes for the presenter:**
- **The learner adds two properties at review.** One is "touching intervals merge"; the
  other is that the output covers exactly the input's points. Without that coverage
  property, an output that drops whole intervals still passes "no overlaps" and "sorted",
  so unsorted input can't break anything. That's worth saying out loud: a spec has to tie
  the output back to the input.
- **The follow-up's wording depends on the gate.** When the model writes it, it reads
  "What if the input intervals are not sorted by start time?". In the recorded run the gate
  replaced it with the plain template question.
- **The code has a second, real bug: it writes into its input.** `merged = [intervals[0]]`
  aliases the caller's first interval, and `merged[-1][1] = …` changes it. With tuple
  intervals this raises `TypeError`. Interval inputs are now drawn as lists, as the intent
  says. Mutating the caller's lists is still a bug, but no property checks for it.

## 2. Binary search that never finishes ([code](binary_search.py), [run](transcripts/search.md))

**Intent:** "Binary search: return the index of target in the sorted list nums, or -1 if
absent."

The bug: `lo = mid` instead of `lo = mid + 1`. Once `lo == mid` and `nums[mid] < target`,
the range never shrinks.

| What to show | What the recorded run did |
|---|---|
| The timeout is handled and shown as a symptom, not a crash | The question is about `binary_search([5], 6)`. The reveal shows "you predicted `-1`" against "**never finishes**: still running after 1.0s". The hunt finished normally in about 13 s. |
| Level 3 trace | `nums = [5], target = 6, lo = 0, hi = 0`, then `mid = 0`, then rows with no change. The hint asks what identical values mean for the loop. |
| The fix | `lo = mid + 1` passed, with 3,029 inputs checked. Follow-up: "What if the input list is not sorted?" |

**How hangs are handled:**
- **Each call is stopped after `EXPLORE_CALL_TIMEOUT_S` (1 s).**
- **After the first hang, later calls are cut much shorter.** The cut is 20 times the
  slowest call that finished, and at least 0.02 s. Without this, every hanging input in the
  search would cost a full second, hundreds of seconds in total.
- **A reported hang is always re-run with the full limit,** so a call that was merely slow
  is never reported as one.
- **The hunt has an overall budget** of 70% of the sandbox's time limit. It stops at that
  point and reports what it found.

## 3. LRU cache whose `get` doesn't count as a use ([code](lru_cache.py), [run](transcripts/lru.md))

**Intent:** "An LRU cache with a fixed capacity. get(key) returns the value or -1;
put(key, value) stores it, evicting the least recently used key when the cache is full."

The bug: `get` returns the value without `self.cache.move_to_end(key)`, so a key that was
just read can still be evicted.

**How a class is tested:**
- **Hypothesis `RuleBasedStateMachine`:** it creates the object, calls `put` and `get` in
  random order with small keys, and checks every property after every call.
- **Shrinking:** a failure shrinks to the shortest call sequence. A final pass then drops
  any call the failure doesn't need, and renumbers keys in order of first use.
- **Tests:** with a capacity of 2, the sequence is exactly the one in the issue:
  `cache = LRUCache(2); cache.put(1, 1); cache.put(2, 1); cache.get(1); cache.put(3, 1); cache.get(1)`.
  The last call returns `-1` where a correct cache returns `1`.

| What to show | What the recorded run did |
|---|---|
| Shrunk operation sequence | The issue's sequence, with the model strategy's keys: "After `cache = LRUCache(2); cache.put(-10, -10); cache.put(1, -10); cache.get(-10); cache.put(2, -10)`, what should `cache.get(-10)` return?" It returned `-1`; a correct cache returns `-10`. Renumbering keeps the values the strategy drew, so here they start at its lower bound, -10, rather than 1. |
| Level 3 trace | Shows the object's state (`self.cache = OrderedDict({-10: -10, 1: -10})`) and marks the `put(2, …)` right after `get(-10)`, which evicts -10 because the get didn't move it to the end. |
| The fix | Adding `self.cache.move_to_end(key)` passed, with 3,007 inputs checked. |

**Notes:**
- **The learner adds the recency rule at review.** The models don't always propose
  "get returns the latest value". Class properties are written by the deep model, because
  the fast one too often wrote checks that rejected correct behaviour.
- **The recorded follow-up was odd.** It asked about `LRUCache(0)`, which counted as a
  failure only because one of the model's properties says "capacity is a positive integer".
  That property judges the input, not the behaviour. Edit or remove a property like that at
  review.
