"""agentd: the agos computer-use control daemon."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agentd")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0"
