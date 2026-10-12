"""Authorizer 的内置参考实现。契约在 ``ctx_weft.protocols.capability``。"""

from ctx_weft.providers.authorizer.allow import AllowAllAuthorizer, AllowListAuthorizer
from ctx_weft.providers.authorizer.human import HumanConfirmationAuthorizer

__all__ = [
    "AllowAllAuthorizer",
    "AllowListAuthorizer",
    "HumanConfirmationAuthorizer",
]
