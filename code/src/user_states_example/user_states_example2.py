from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never


@dataclass(frozen=True)
class Active:
    session_id: str
    user_id: str
    email: str
    loyalty_points: int
    shipping_address: str


@dataclass(frozen=True)
class PendingVerification:
    session_id: str
    email: str
    verification_token: str


@dataclass(frozen=True)
class Guest:
    session_id: str


type User = Active | PendingVerification | Guest


def process_user(user: User):
    match user:
        case Active(
            email=email,
            user_id=user_id,
            loyalty_points=loyalty_points,
            shipping_address=shipping_address,
        ):
            send_receipt(email)
            give_discount(user_id, loyalty_points)
            ship_to(shipping_address)
        case PendingVerification(email, token):
            send_verification_email(email, token)
        case Guest():
            show_guest_banner()
        case _ as unreachable:
            assert_never(unreachable)


def show_guest_banner(): ...
def send_verification_email(email: str, token: str): ...
def send_receipt(email: str): ...
def give_discount(user_id: str, loyalty_points: int): ...
def ship_to(address: str): ...
