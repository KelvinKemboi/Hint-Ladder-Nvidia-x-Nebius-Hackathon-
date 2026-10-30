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
- `  70.7s` **proposed**: 4 properties for `LRUCache`
  - get returns the value most recently put for that key, or -1 if the key has never been put or has been evicted.
  - put returns None and does not affect the cache size beyond adding or updating the key.
  - The cache never holds more than capacity items at any point.
  - When a key is accessed via get, it becomes the most recently used, so it is not evicted before keys that have not been accessed since.

## The hunt
- `  70.7s` **learner adds**: get(key) returns the value most recently put for key, or -1 if it was never put or was evicted; a get counts as a use, so the evicted key is always the one least recently put or read.
- `  70.7s` **progress**: Reading the LRUCache class and proposing properties
- `  70.7s` **progress**: Proposed 4 properties
- `  70.7s` **progress**: Writing checks for 1 edited properties
- `  79.4s` **progress**: Writing an input generator
- ` 102.6s` **progress**: Testing 5 properties on 200 generated call sequences each, plus 0 edge cases
- ` 119.5s` **progress**: Found 1 distinct failure(s)
- ` 119.5s` **progress**: Working out the root cause
- ` 140.7s` **progress**: Root cause found and fix verified
- ` 142.9s` **question**: After `cache = LRUCache(2); cache.put(-10, -10); cache.put(1, -10); cache.get(-10); cache.put(2, -10)`, what should `cache.get(-10)` return?
- ` 144.7s` **prediction**: `-10` (what a correct version returns)
- ` 144.7s` **reveal**: revealed: predicted -10 vs value -1
> You expected `-10`. The code returns `-1`. Your prediction fits the spec and the code's answer doesn't: that gap is the bug.

- ` 151.9s` **hint level 2**: The bug is in the get method.

- ` 165.4s` **hint level 3**: Here's what the variables hold as it runs:
```
…
   7  self.capacity = 2, self.cache = OrderedDict({-10: -10}), key = 1, value = -10
   8  self.cache = OrderedDict({-10: -10, 1: -10})
   9  key = -10
  10  (no change)
  11  self.cache = OrderedDict({-10: -10, 1: -10}), key = 2, value = -10   ◀ goes wrong here
  12  (no change)
  13  self.cache = OrderedDict({-10: -10, 1: -10, 2: -10})
      → returns [None, None, None, -10, None, -1]
```

## The fix
```diff
@@ -14,2 +14,3 @@
             return -1
+        self.cache.move_to_end(key)
         return self.cache[key]
```
- ` 165.5s` **progress**: Checking your fix on 500 fresh inputs per property
- ` 172.7s` **progress**: Verified on 100/500 fresh inputs
- ` 172.9s` **progress**: Verified on 200/500 fresh inputs
- ` 173.0s` **progress**: Verified on 300/500 fresh inputs
- ` 173.1s` **progress**: Verified on 400/500 fresh inputs
- ` 173.2s` **progress**: Verified on 500/500 fresh inputs
- ` 184.9s` **fix**: passed (3007 inputs checked)
> Your fix holds: every property passed on the original failing inputs and 3007 generated ones.
- ` 184.9s` **follow-up**: What happens if the cache capacity is zero?

## Post-mortem
```json
{
  "bug_class": "missing edge case",
  "bug_classes": [
    "missing edge case"
  ],
  "how_to_spot": "In an LRU cache, every access (get or put) must update the recency of the key; verify that both methods move the key to the most-recent position.",
  "other_edge_cases": [
    "Cache capacity of zero or negative (should handle gracefully or raise an error).",
    "Repeated puts of the same key without intervening gets (should not cause duplicate entries).",
    "Interleaved gets and puts that test eviction order after multiple accesses.",
    "Getting a key that was just evicted (should return -1)."
  ],
  "hint_level_reached": 3,
  "fix_attempts": 1,
  "inputs_checked": 3007,
  "cases": [
    {
      "call": "cache = LRUCache(2); cache.put(-10, -10); cache.put(1, -10); cache.get(-10); cache.put(2, -10); cache.get(-10)",
      "bug_class": "missing edge case",
      "hint_level": 3
    }
  ]
}
```
