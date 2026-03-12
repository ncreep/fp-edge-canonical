from __future__ import annotations

from error_types import *
from step2_functional.action_types import PasswordActions

from result import *


def handle_errors(actions: PasswordActions, error: ProcessError) -> None: ...
