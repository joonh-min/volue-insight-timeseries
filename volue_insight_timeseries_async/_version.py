from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("volue-insight-timeseries-async")
except PackageNotFoundError:
    # The source tree is used without installing the package
    __version__ = "0+unknown"
