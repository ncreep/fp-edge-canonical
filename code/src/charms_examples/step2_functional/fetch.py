from __future__ import annotations

from typing import Protocol

from error_types import *
from input_types import *

from result import *


class ConfigSetupFetcher(Protocol):
    def fetch(self) -> Result[FetchError, ConfigInput]: ...
