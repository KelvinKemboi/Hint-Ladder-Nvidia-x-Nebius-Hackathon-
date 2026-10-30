# Demo 3: LRU cache whose get() doesn't count as a use

Intent: *An LRU cache with a fixed capacity. get(key) returns the value or -1; put(key, value) stores it, evicting the least recently used key when the cache is full.*

```python
from collections import OrderedDict


class LRUCache:
    """A fixed-size cache that evicts the least recently used key when it is full."""

    def __init__(self, capacity):
        self.capacity = capacity
        self.cache = OrderedDict()

    def get(self, key):
        """Return the value stored for key, or -1 if it isn't cached."""
        if key not in self.cache:
            return -1
        return self.cache[key]

    def put(self, key, value):
        """Store value under key, evicting the least recently used key if over capacity."""
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)
```

## Properties
- `  33.6s` **proposed**: 3 properties for `LRUCache`
  - For every get operation, the returned value equals the value most recently put for that key, or -1 if the key has never been put or has been evicted.
  - Every put operation returns None.
  - The LRUCache constructor returns None and the capacity is a positive integer.

## The hunt
- `  33.6s` **learner adds**: get(key) returns the value most recently put for key, or -1 if it was never put or was evicted; a get counts as a use, so the evicted key is always the one least recently put or read.
- `  33.6s` **progress**: Reading the LRUCache class and proposing properties
- `  33.6s` **progress**: Proposed 3 properties
- `  33.6s` **progress**: Writing checks for 1 edited properties
- `  52.6s` **progress**: Writing an input generator
- `  82.0s` **progress**: Testing 4 properties on 200 generated call sequences each, plus 0 edge cases
- `  88.9s` **progress**: Found 1 distinct failure(s)
- `  88.9s` **progress**: Working out the root cause
- ` 100.0s` **progress**: Root cause found and fix verified
- ` 101.2s` **question**: After `cache = LRUCache(3); cache.put(1, 1); cache.put(2, 1); cache.get(1); cache.put(3, 1); cache.put(4, 1)`, what should `cache.get(1)` return?
- ` 101.4s` **prediction**: `1` (what a correct version returns)
- ` 101.4s` **reveal**: revealed: predicted 1 vs value -1
> You expected `1`. The code returns `-1`. Your prediction fits the spec and the code's answer doesn't: that gap is the bug.

- ` 107.6s` **hint level 2**: The bug is in the get method.

- ` 115.7s` **hint level 3**: At the step where the arrow points, the cache contains keys 1, 2, and 3, but key 1 was read earlier. What did you expect the key order to be at that moment?
```
…
   7  self.capacity = 3, self.cache = OrderedDict({1: 1}), key = 2, value = 1
   8  self.cache = OrderedDict({1: 1, 2: 1})
   9  key = 1
  10  (no change)
  11  self.cache = OrderedDict({1: 1, 2: 1}), key = 3, value = 1   ◀ goes wrong here
  12  (no change)
  13  self.cache = OrderedDict({1: 1, 2: 1, 3: 1})
      → returns [None, None, None, 1, None, None, -1]
```

## The fix
```diff
@@ -14,2 +14,3 @@
             return -1
+        self.cache.move_to_end(key)
         return self.cache[key]
```
- ` 115.7s` **progress**: Checking your fix on 500 fresh inputs per property
- ` 118.1s` **progress**: Verified on 100/500 fresh inputs
- ` 118.2s` **progress**: Verified on 200/500 fresh inputs
- ` 118.3s` **progress**: Verified on 300/500 fresh inputs
- ` 118.3s` **progress**: Verified on 400/500 fresh inputs
- ` 118.4s` **progress**: Verified on 500/500 fresh inputs
- ` 132.0s` **fix**: passed (2507 inputs checked)
> Your fix holds: every property passed on the original failing inputs and 2507 generated ones.
- ` 132.0s` **follow-up**: After `cache = LRUCache(0); cache.put('a', 1)`, what should `cache.get('a')` return?
- ` 132.0s` **progress**: Working out the root cause

## Post-mortem
```json
{
  "bug_class": "missing edge case",
  "bug_classes": [
    "missing edge case"
  ],
  "how_to_spot": "In LRU caches, every access (read or write) must update recency; check that get() calls move_to_end() just like put() does for existing keys.",
  "other_edge_cases": [
    "capacity <= 0 should either raise or behave as a no-op cache",
    "put() on an existing key must refresh its position before updating the value",
    "eviction should trigger only when size exceeds capacity, not when equal",
    "concurrent get/put calls could corrupt the OrderedDict without locking"
  ],
  "hint_level_reached": 3,
  "fix_attempts": 1,
  "inputs_checked": 2507,
  "cases": [
    {
      "call": "cache = LRUCache(3); cache.put(1, 1); cache.put(2, 1); cache.get(1); cache.put(3, 1); cache.put(4, 1); cache.get(1)",
      "bug_class": "missing edge case",
      "hint_level": 3
    }
  ]
}
```

## The follow-up breaks it again
- ` 132.0s` **question**: After `cache = LRUCache(0); cache.put('a', 1)`, what should `cache.get('a')` return?
- ` 132.2s` **prediction**: `-1` (what a correct version returns)
- ` 132.2s` **reveal**: matches_code: predicted -1 vs value -1
> That's exactly what the code does: it returns `-1`. But does that fit your spec? Check it against: "The LRUCache constructor returns None and the capacity is a positive integer".
