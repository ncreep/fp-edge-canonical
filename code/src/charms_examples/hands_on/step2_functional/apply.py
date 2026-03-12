from __future__ import annotations

from error_types import *
from step2_functional.action_types import PasswordActions
from step2_functional.output_types import *
from type_defs import PasswordOutcome

from result import *


def apply_handler(
    actions: PasswordActions, outcome: PasswordOutcome
) -> Result[ApplyError, None]: ...
