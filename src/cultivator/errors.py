class CultivatorError(Exception):
    """Base class for errors that should be reported without a traceback."""


class ManifestError(CultivatorError):
    pass


class GraphError(CultivatorError):
    pass


class StateError(CultivatorError):
    pass


class GitError(CultivatorError):
    pass
