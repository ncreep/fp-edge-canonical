from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, cast


@dataclass(frozen=True)
class User:
    session_id: str
    # if user_id is set, the account is verified
    # email must be present
    user_id: Optional[str] = None
    # if email is set, the user has registered
    email: Optional[str] = None
    # only meaningful for pending verification
    # must be present if email is set
    # but not for a verified user
    verification_token: Optional[str] = None
    # only meaningful for active users
    # must be present if user_id is set
    loyalty_points: Optional[int] = None
    shipping_address: Optional[str] = None


def process_user_record1(user: User):
    if user.user_id:
        send_receipt(cast(str, user.email))
        give_discount(user.user_id, cast(int, user.loyalty_points))
        ship_to(cast(str, user.shipping_address))
    elif user.email:
        send_verification_email(user.email, cast(str, user.verification_token))
    else:
        show_guest_banner()


def process_user_record2(user: User):
    if user.user_id:
        assert user.email is not None
        assert user.loyalty_points is not None
        assert user.shipping_address is not None

        send_receipt(user.email)
        give_discount(user.user_id, user.loyalty_points)
        ship_to(user.shipping_address)
    elif user.email:
        assert user.verification_token is not None

        send_verification_email(user.email, user.verification_token)
    else:
        show_guest_banner()


def show_guest_banner(): ...
def send_verification_email(email: str, token: str): ...
def send_receipt(email: str): ...
def give_discount(user_id: str, loyalty_points: int): ...
def ship_to(address: str): ...
