from __future__ import annotations

from typing import Protocol

from error_types import *
from type_defs import *

from result import *


class PasswordFetcher(Protocol):
    def fetch(self) -> Result[FetchError, PasswordInput]: ...
