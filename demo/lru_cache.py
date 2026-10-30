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
