# Demo 1: AI-written merge_intervals

Intent: *Merge overlapping intervals, given as [start, end] lists sorted by start.*

```python
def merge_intervals(intervals):
    if not intervals:
        return []
    
    merged = [intervals[0]]
    
    for current in intervals[1:]:
        last = merged[-1]
        if current[0] < last[1]:  # overlap: current starts before last ends
            merged[-1][1] = max(last[1], current[1])  # merge by extending end time
        else:
            merged.append(current)  # no overlap, add current interval
    
    return merged
```

## Properties
- `  23.6s` **proposed**: 4 properties for `merge_intervals`
  - The output intervals are sorted by their start times.
  - No two output intervals overlap, including sharing only an endpoint.
  - The number of output intervals is never greater than the number of input intervals.
  - All output intervals have numeric start and end values.

## The hunt
- `  23.6s` **learner adds**: No two output intervals overlap or touch: [1, 4] and [4, 5] merge into [1, 5].
- `  23.6s` **learner adds**: Every point covered by an input interval is covered by an output interval, and no other point is.
- `  23.6s` **progress**: Reading merge_intervals() and proposing properties
- `  23.6s` **progress**: Proposed 4 properties
- `  23.6s` **progress**: Writing checks for 2 edited properties
- `  62.9s` **progress**: Writing an input generator
- ` 107.4s` **progress**: Testing 6 properties on 200 generated inputs each, plus 9 edge cases
- ` 107.9s` **progress**: Found 1 distinct failure(s)
- ` 107.9s` **progress**: Working out the root cause
- ` 110.9s` **progress**: Root cause found and fix verified
- ` 111.1s` **question**: What should `merge_intervals([[1, 4], [4, 5]])` return?
- ` 111.4s` **prediction**: `[[1, 5]]` (what a correct version returns)
- ` 111.4s` **reveal**: revealed: predicted [[1, 5]] vs value [[1, 4], [4, 5]]
> You expected `[[1, 5]]`. The code returns `[[1, 4], [4, 5]]`. Your prediction fits the spec and the code's answer doesn't: that gap is the bug.

- ` 115.5s` **hint level 2**: The bug lives in the merge_intervals function. Check how it determines whether the current interval should be combined with the last one in the merged list.

- ` 133.1s` **hint level 3**: Here's what the variables hold while `merge_intervals([[1, 4], [4, 5]])` runs. Look at the row marked ◀.
```
…
   3  intervals = [[1, 4], [4, 5]], merged = [[1, 4]]
   4  current = [4, 5]
   5  last = [1, 4]
   6  (no change)
   7  intervals = [[1, 4], [4, 5]], merged = [[1, 4], [4, 5]], last = [1, 4]   ◀ goes wrong here
   8  (no change)
      → returns [[1, 4], [4, 5]]
```

## The fix
```diff
@@ -8,3 +8,3 @@
         last = merged[-1]
-        if current[0] < last[1]:  # overlap: current starts before last ends
+        if current[0] <= last[1]:  # overlap: current starts before last ends
             merged[-1][1] = max(last[1], current[1])  # merge by extending end time
```
- ` 133.2s` **progress**: Checking your fix on 500 fresh inputs per property
- ` 133.8s` **progress**: Verified on 100/500 fresh inputs
- ` 133.8s` **progress**: Verified on 200/500 fresh inputs
- ` 133.9s` **progress**: Verified on 300/500 fresh inputs
- ` 133.9s` **progress**: Verified on 400/500 fresh inputs
- ` 133.9s` **progress**: Verified on 500/500 fresh inputs
- ` 142.6s` **fix**: passed (3516 inputs checked)
> Your fix holds: every property passed on the original failing inputs and 3516 generated ones.
- ` 142.6s` **follow-up**: What should `merge_intervals([[5, 6], [1, 3], [2, 4]])` return?
- ` 142.6s` **progress**: Working out the root cause

## Post-mortem
```json
{
  "bug_class": "boundary condition",
  "bug_classes": [
    "boundary condition"
  ],
  "how_to_spot": "When intervals are defined as inclusive (closed), the overlap check must use <= to merge adjacent intervals that share an endpoint; always test with touching endpoints like [1,4] and [4,5] to catch this.",
  "other_edge_cases": [
    "Nested intervals where one is completely inside another (e.g., [[1,10], [2,3]])",
    "Identical intervals (e.g., [[1,2], [1,2]])",
    "Zero-length intervals (e.g., [[5,5], [5,6]])",
    "Unsorted input violating the precondition that intervals are sorted by start"
  ],
  "hint_level_reached": 3,
  "fix_attempts": 1,
  "inputs_checked": 3516,
  "cases": [
    {
      "call": "merge_intervals([[1, 4], [4, 5]])",
      "bug_class": "boundary condition",
      "hint_level": 3
    }
  ]
}
```

## The follow-up breaks it again
- ` 142.6s` **question**: What should `merge_intervals([[5, 6], [1, 3], [2, 4]])` return?
- ` 142.9s` **prediction**: `[[1, 4], [5, 6]]` (what a correct version returns)
- ` 142.9s` **reveal**: revealed: predicted [[1, 4], [5, 6]] vs value [[5, 6]]
> You expected `[[1, 4], [5, 6]]`. The code returns `[[5, 6]]`. Your prediction fits the spec and the code's answer doesn't: that gap is the bug.
