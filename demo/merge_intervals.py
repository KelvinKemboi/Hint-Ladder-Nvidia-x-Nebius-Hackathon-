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
