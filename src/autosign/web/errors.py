from fastapi import HTTPException

from autosign.core.services.accounts import AccountNotFoundError


def account_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AccountNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))
