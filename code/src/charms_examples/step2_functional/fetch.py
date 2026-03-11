from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from error_types import *

from result import *

if TYPE_CHECKING:
    pass
from input_types import *


class ConfigSetupFetcher(Protocol):
    def fetch(self) -> Result[FetchError, ConfigInput]: ...
