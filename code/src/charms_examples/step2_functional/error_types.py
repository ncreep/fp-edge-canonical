from ops.pebble import Layer


class FetchError(Exception):
    pass


class ConfigError(Exception):
    pass


class ConfigErrors(ExceptionGroup[ConfigError]):
    pass


class ConfigPushError(Exception):
    pass


class ServiceUpdateError(Exception):
    pass


class ReplanError(Exception):
    def __init__(self, layer: Layer, cause: BaseException) -> None:
        self.__cause__ = cause
        self.layer = layer


class ReloadError(Exception):
    pass


class ConfigTimeoutError(Exception):
    pass


type ApplyError = ConfigPushError | ServiceUpdateError | ReplanError | ReloadError

type ProcessError = FetchError | ConfigErrors | ApplyError
