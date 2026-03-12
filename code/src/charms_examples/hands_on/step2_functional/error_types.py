from charm import *
from ops import ModelError, SecretNotFoundError

type FetchError = SecretNotFoundError | ModelError

type ApplyError = IdentityNotExistsError | ClientRequestError

type ProcessError = FetchError | ApplyError
