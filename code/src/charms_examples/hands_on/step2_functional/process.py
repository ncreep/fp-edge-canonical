from __future__ import annotations

from fetch import PasswordFetcher

from charms_examples.hands_on.step2_functional.action_types import PasswordActions


def process(fetcher: PasswordFetcher, actions: PasswordActions) -> None: ...
