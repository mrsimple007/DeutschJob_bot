"""Symmetric encryption for stored SMTP passwords."""
import os
from cryptography.fernet import Fernet

_key = os.environ.get("ENCRYPTION_KEY")
if not _key:
    raise RuntimeError("ENCRYPTION_KEY is not set. Generate one with: "
                       "python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
_f = Fernet(_key.encode())


def encrypt(plain: str) -> str:
    return _f.encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _f.decrypt(token.encode()).decode()