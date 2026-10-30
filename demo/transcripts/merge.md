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
- `  65.7s` **proposed**: 4 properties for `merge_intervals`
  - Output intervals are sorted by start and are pairwise disjoint (no overlap, even at endpoints).
  - The first output interval starts at the minimum start among all input intervals.
  - The last output interval ends at the maximum end among all input intervals.
  - Every output interval's start equals the start of some input interval, and every output interval's end equals the end of some input interval.

## The hunt
- `  65.7s` **learner adds**: No two output intervals overlap or touch: [1, 4] and [4, 5] merge into [1, 5].
- `  65.7s` **learner adds**: Every point covered by an input interval is covered by an output interval, and no other point is.
- `  65.8s` **progress**: Reading merge_intervals() and proposing properties
- `  65.8s` **progress**: Proposed 4 properties
- `  65.8s` **progress**: Writing checks for 2 edited properties
- ` 140.1s` **progress**: Writing an input generator
- ` 320.9s` **progress**: Testing 6 properties on 200 generated inputs each, plus 9 edge cases
- ` 323.5s` **progress**: Found 1 distinct failure(s)
- ` 323.5s` **progress**: Working out the root cause
- ` 336.8s` **progress**: Root cause found and fix verified
- ` 337.3s` **question**: What should `merge_intervals([[1, 4], [4, 5]])` return?
- ` 338.9s` **prediction**: `[[1, 5]]` (what a correct version returns)
- ` 338.9s` **reveal**: revealed: predicted [[1, 5]] vs value [[1, 4], [4, 5]]
> You expected `[[1, 5]]`. The code returns `[[1, 4], [4, 5]]`. Your prediction fits the spec and the code's answer doesn't: that gap is the bug.

- ` 351.5s` **hint level 2**: The bug is in the loop body where you decide whether to merge or append the current interval.

- ` 367.5s` **hint level 3**: Here's what the variables hold while `merge_intervals([[1, 4], [4, 5]])` runs. Look at the row marked ◀.
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
- ` 367.5s` **progress**: Checking your fix on 500 fresh inputs per property
- ` 369.7s` **progress**: Verified on 100/500 fresh inputs
- ` 370.4s` **progress**: Verified on 200/500 fresh inputs
- ` 370.4s` **progress**: Verified on 300/500 fresh inputs
- ` 370.5s` **progress**: Verified on 400/500 fresh inputs
- ` 370.7s` **progress**: Verified on 500/500 fresh inputs
- ` 379.8s` **fix**: passed (3516 inputs checked)
> Your fix holds: every property passed on the original failing inputs and 3516 generated ones.
- ` 379.8s` **follow-up**: What should `merge_intervals([[5, 7], [1, 3], [2, 6]])` return?
- ` 379.8s` **progress**: Working out the root cause

## Post-mortem
```json
{
  "bug_class": "boundary condition",
  "bug_classes": [
    "boundary condition"
  ],
  "how_to_spot": "Check whether intervals are open or closed at endpoints; test cases where intervals touch exactly (e.g., [1,4] and [4,5]) to catch off-by-one overlaps.",
  "other_edge_cases": [
    "Single interval input",
    "All intervals overlapping into one",
    "Intervals completely contained within others",
    "Empty input list"
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
- ` 379.8s` **question**: What should `merge_intervals([[5, 7], [1, 3], [2, 6]])` return?
- ` 381.9s` **prediction**: `[[1, 7]]` (what a correct version returns)
- ` 381.9s` **reveal**: revealed: predicted [[1, 7]] vs value [[5, 7]]
> You expected `[[1, 7]]`. The code returns `[[5, 7]]`. Your prediction fits the spec and the code's answer doesn't: that gap is the bug.
