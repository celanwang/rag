from hashlib import sha256


def key_digest(key: str) -> bytes:
    return sha256(key.encode()).digest()
