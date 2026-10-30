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
- `  17.1s` **proposed**: 5 properties for `binary_search`
  - The result is either -1 or a valid index of nums.
  - If the result is not -1, the element at that index in nums equals target.
  - If target is not in nums then the result must be -1.
  - If nums is empty then the result must be -1.
  - The result is always an integer.

## The hunt
- `  17.1s` **progress**: Reading binary_search() and proposing properties
- `  17.1s` **progress**: Proposed 5 properties
- `  17.1s` **progress**: Writing an input generator
- `  27.1s` **progress**: Testing 5 properties on 200 generated inputs each, plus 22 edge cases
- `  40.2s` **progress**: Found 1 distinct failure(s)
- `  40.2s` **progress**: Working out the root cause
- `  52.6s` **progress**: Root cause found and fix verified
- `  53.2s` **question**: What should `binary_search([5], 6)` return?
- `  55.8s` **prediction**: `-1` (what a correct version returns)
- `  55.8s` **reveal**: revealed: predicted -1 vs timeout still running after 1.0s
> You expected `-1`. The code never finishes: it was still running when stopped after 1.0s. Your prediction fits the spec and the code's answer doesn't: that gap is the bug.

- `  71.2s` **hint level 2**: The problem is inside binary_search function.

- `  89.0s` **hint level 3**: Here's what the variables hold while `binary_search([5], 6)` runs. Look at the row marked ◀.
```
…
   2  nums = [5], target = 6, lo = 0, hi = 0
   3  (no change)
   4  mid = 0
   5  (no change)
   6  (no change)   ◀ goes wrong here
   7  (no change)
   8  (no change)
      → raises TimeoutError: did not finish within 1.0s
```

## The fix
```diff
@@ -8,3 +8,3 @@
         if nums[mid] < target:
-            lo = mid
+            lo = mid + 1
         else:
```
- `  89.0s` **progress**: Checking your fix on 500 fresh inputs per property
- `  91.3s` **progress**: Verified on 100/500 fresh inputs
- `  92.0s` **progress**: Verified on 200/500 fresh inputs
- `  92.0s` **progress**: Verified on 300/500 fresh inputs
- `  92.1s` **progress**: Verified on 400/500 fresh inputs
- `  92.1s` **progress**: Verified on 500/500 fresh inputs
- ` 102.7s` **fix**: passed (3029 inputs checked)
> Your fix holds: every property passed on the original failing inputs and 3029 generated ones.
- ` 102.7s` **follow-up**: What if the input list is not sorted?

## Post-mortem
```json
{
  "bug_class": "off-by-one",
  "bug_classes": [
    "off-by-one"
  ],
  "how_to_spot": "When updating search bounds, verify that the new bound strictly excludes the midpoint; otherwise a single-element interval can stall. Test with one-element arrays where the target is absent to catch infinite loops.",
  "other_edge_cases": [
    "Empty list (len(nums) == 0)",
    "Target smaller than all elements",
    "Target larger than all elements",
    "Target at the very first or last index"
  ],
  "hint_level_reached": 3,
  "fix_attempts": 1,
  "inputs_checked": 3029,
  "cases": [
    {
      "call": "binary_search([5], 6)",
      "bug_class": "off-by-one",
      "hint_level": 3
    }
  ]
}
```
