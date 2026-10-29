class GenerationError(Exception):
    """The model could not produce something usable, even after retries."""


class NotAvailable(Exception):
    """The step can't be taken right now (e.g. no hints left, or no diagnosis)."""


class UserCodeError(Exception):
    """The user's code could not be run (fails to import, or times out on import)."""
