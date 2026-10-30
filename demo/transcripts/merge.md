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
- `  52.8s` **proposed**: 2 properties for `merge_intervals`
  - Output intervals never overlap (they are pairwise disjoint).
  - Output intervals are sorted by their start values.

## The hunt
- `  52.8s` **learner adds**: No two output intervals overlap or touch: [1, 4] and [4, 5] merge into [1, 5].
- `  52.8s` **learner adds**: Every point covered by an input interval is covered by an output interval, and no other point is.
- `  52.8s` **progress**: Reading merge_intervals() and proposing properties
- `  52.8s` **progress**: Proposed 2 properties
- `  52.8s` **progress**: Writing checks for 2 edited properties
- `  79.1s` **progress**: Writing an input generator
- ` 118.3s` **progress**: Testing 4 properties on 200 generated inputs each, plus 9 edge cases
- ` 120.9s` **progress**: Found 1 distinct failure(s)
- ` 120.9s` **progress**: Working out the root cause
- ` 128.5s` **progress**: Root cause found and fix verified
- ` 129.4s` **question**: What should `merge_intervals([[1, 4], [4, 5]])` return?
- ` 131.2s` **prediction**: `[[1, 5]]` (what a correct version returns)
- ` 131.2s` **reveal**: revealed: predicted [[1, 5]] vs value [[1, 4], [4, 5]]
> You expected `[[1, 5]]`. The code returns `[[1, 4], [4, 5]]`. Your prediction fits the spec and the code's answer doesn't: that gap is the bug.

- ` 139.5s` **hint level 2**: The problem is in the merge_intervals function, within the loop that processes each interval. Examine how the code decides if the current interval should be merged with the last one. What does that decision currently require?

- ` 146.6s` **hint level 3**: Look at the row marked ◀ where the merged list becomes [[1, 4], [4, 5]]; what did you expect the values of `last` and `current` to be at that point?
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
- ` 146.6s` **progress**: Checking your fix on 500 fresh inputs per property
- ` 148.7s` **progress**: Verified on 100/500 fresh inputs
- ` 149.5s` **progress**: Verified on 200/500 fresh inputs
- ` 149.5s` **progress**: Verified on 300/500 fresh inputs
- ` 149.6s` **progress**: Verified on 400/500 fresh inputs
- ` 149.6s` **progress**: Verified on 500/500 fresh inputs
- ` 161.4s` **fix**: passed (2516 inputs checked)
> Your fix holds: every property passed on the original failing inputs and 2516 generated ones.
- ` 161.4s` **follow-up**: What if the input intervals are tuples instead of lists?

## Post-mortem
```json
{
  "bug_class": "boundary condition",
  "bug_classes": [
    "boundary condition"
  ],
  "how_to_spot": "Check whether intervals or ranges are defined as inclusive [start, end] or half-open [start, end); if inclusive, adjacent intervals sharing an endpoint (e.g., [1,4] and [4,5]) must be treated as overlapping, so use <= instead of <.",
  "other_edge_cases": [
    "Empty input list",
    "Single interval input",
    "Intervals completely contained within another (e.g., [1,10], [3,5])",
    "Unsorted input (though spec says sorted, defensive checks help)"
  ],
  "hint_level_reached": 3,
  "fix_attempts": 1,
  "inputs_checked": 2516,
  "cases": [
    {
      "call": "merge_intervals([[1, 4], [4, 5]])",
      "bug_class": "boundary condition",
      "hint_level": 3
    }
  ]
}
```
