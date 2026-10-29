class GenerationError(Exception):
    """The model could not produce something usable, even after retries."""


class UserCodeError(Exception):
    """The user's code could not be run (fails to import, or times out on import)."""
