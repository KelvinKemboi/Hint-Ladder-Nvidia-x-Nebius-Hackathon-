# Demo 2: binary search that can stop making progress

Intent: *Binary search: return the index of target in the sorted list nums, or -1 if absent.*

```python
def binary_search(nums, target):
    """Return the index of target in the sorted list nums, or -1 if it isn't there."""
    lo, hi = 0, len(nums) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if nums[mid] == target:
            return mid
        if nums[mid] < target:
            lo = mid
        else:
            hi = mid - 1
    return -1
```

## Properties
- `  16.0s` **proposed**: 4 properties for `binary_search`
  - The returned index, if not -1, must point to an occurrence of target in nums.
  - If target is not present in nums, the function must return -1.
  - The function never returns an index greater than or equal to the length of nums.
  - When nums is empty, the function must return -1.

## The hunt
- `  16.1s` **progress**: Reading binary_search() and proposing properties
- `  16.1s` **progress**: Proposed 4 properties
- `  16.1s` **progress**: Writing an input generator
- `  36.9s` **progress**: Testing 4 properties on 200 generated inputs each, plus 22 edge cases
- `  46.1s` **progress**: Found 1 distinct failure(s)
- `  46.1s` **progress**: Working out the root cause
- `  50.2s` **progress**: Root cause found and fix verified
- `  50.2s` **question**: What should `binary_search([5], 6)` return?
- `  51.4s` **prediction**: `-1` (what a correct version returns)
- `  51.4s` **reveal**: revealed: predicted -1 vs timeout still running after 1.0s
> You expected `-1`. The code never finishes: it was still running when stopped after 1.0s. Your prediction fits the spec and the code's answer doesn't: that gap is the bug.

- `  56.5s` **hint level 2**: The problem is in the binary_search function, specifically within its while loop. Can you see where the search range might not be narrowing correctly?

- `  69.5s` **hint level 3**: At the marked row, the variable values are identical to the previous step. What does that mean for the loop condition on the next iteration?
```
…
   2  nums = [5], target = 6, lo = 0, hi = 0
   3  (no change)
   4  mid = 0
   5  (no change)
   6  (no change)
   7  (no change)
   8  (no change)
      - raises TimeoutError: did not finish within 1.0s
```

## The fix
```diff
@@ -8,3 +8,3 @@
         if nums[mid] < target:
-            lo = mid
+            lo = mid + 1
         else:
```
- `  69.5s` **progress**: Checking your fix on 500 fresh inputs per property
- `  70.1s` **progress**: Verified on 100/500 fresh inputs
- `  70.1s` **progress**: Verified on 200/500 fresh inputs
- `  70.1s` **progress**: Verified on 300/500 fresh inputs
- `  70.1s` **progress**: Verified on 400/500 fresh inputs
- `  70.1s` **progress**: Verified on 500/500 fresh inputs
- `  74.7s` **fix**: passed (2529 inputs checked)
> Your fix holds: every property passed on the original failing inputs and 2529 generated ones.
- `  74.7s` **follow-up**: What if the input list is not sorted?

## Post-mortem
```json
{
  "bug_class": "off-by-one",
  "bug_classes": [
    "off-by-one"
  ],
  "how_to_spot": "If the loop condition is lo <= hi but the update sets lo = mid (or hi = mid) instead of moving past mid, the interval may not shrink when lo == hi, causing an infinite loop; always adjust bounds to exclude the checked mid.",
  "other_edge_cases": [
    "Empty list (len(nums) == 0)",
    "Target equals the first element",
    "Target equals the last element",
    "List with duplicate target values"
  ],
  "hint_level_reached": 3,
  "fix_attempts": 1,
  "inputs_checked": 2529,
  "cases": [
    {
      "call": "binary_search([5], 6)",
      "bug_class": "off-by-one",
      "hint_level": 3
    }
  ]
}
```
