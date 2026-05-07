import enum
import hashlib
import hmac
import json
import os
import struct
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


def generate_server_seed() -> str:
    return os.urandom(32).hex()


def commit(server_seed: str) -> str:
    return hashlib.sha256(server_seed.encode()).hexdigest()


def verify(server_seed: str, commitment: str) -> bool:
    return hmac.compare_digest(commit(server_seed), commitment)


_UINT64_MAX = 1 << 64


def _rejection_sample(value_gen, num_outcomes: int) -> int:
    """Return an unbiased value in [0, num_outcomes) using rejection sampling.

    Plain value % N is biased when 2^64 % N != 0. We discard values in the
    tail region [cutoff, 2^64) and rehash with an incrementing counter until
    we land below the cutoff. Expected iterations: < 1.0000000001 for any N.
    """
    cutoff = _UINT64_MAX - (_UINT64_MAX % num_outcomes)
    counter = 0
    while True:
        value = value_gen(counter)
        if value < cutoff:
            return value % num_outcomes
        counter += 1


def resolve(server_seed: str, client_seed: str, num_outcomes: int) -> int:
    """Per-bet resolution using both seeds (slots model — kept for compatibility)."""
    combined = f"{server_seed}:{client_seed}".encode()

    def gen(counter: int) -> int:
        msg = combined + counter.to_bytes(4, "big")
        digest = hmac.new(server_seed.encode(), msg, hashlib.sha256).digest()
        return struct.unpack(">Q", digest[:8])[0]

    return _rejection_sample(gen, num_outcomes)


def resolve_run(server_seed: str, num_outcomes: int) -> int:
    """Single-seed resolution for scheduled runs (roulette model).

    Uses HMAC(server_seed, "a2a:outcome:<counter>") so the outcome cannot be
    derived from the publicly-visible commitment SHA256(server_seed). Rejection
    sampling ensures no modular bias for any number of outcomes.
    """
    key = server_seed.encode()

    def gen(counter: int) -> int:
        msg = b"a2a:outcome:" + counter.to_bytes(4, "big")
        digest = hmac.new(key, msg, hashlib.sha256).digest()
        return struct.unpack(">Q", digest[:8])[0]

    return _rejection_sample(gen, num_outcomes)


def generate_client_seed() -> str:
    return os.urandom(16).hex()


def canonical_bytes(obj: dict) -> bytes:
    """Deterministic JSON serialization for signing. Handles enums and basic types."""
    def _default(o):
        if isinstance(o, enum.Enum):
            return o.value
        raise TypeError(f"Object of type {type(o)} is not JSON serializable")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default).encode()


def generate_keypair() -> tuple[bytes, bytes]:
    """Returns (private_key_bytes, public_key_bytes), each 32 bytes."""
    private = Ed25519PrivateKey.generate()
    priv = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv, pub


def sign(private_key_bytes: bytes, message: bytes) -> bytes:
    """Sign message with Ed25519. Returns 64-byte signature."""
    return Ed25519PrivateKey.from_private_bytes(private_key_bytes).sign(message)


def verify_sig(public_key_bytes: bytes, message: bytes, signature: bytes) -> bool:
    """Verify an Ed25519 signature. Returns False on any failure."""
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature, message)
        return True
    except (InvalidSignature, Exception):
        return False


def load_or_create_keypair(path: Path) -> tuple[bytes, bytes]:
    """Load keypair from <path>.key or generate and save a new one.
    File stores 64 raw bytes (32 private + 32 public) as lowercase hex."""
    key_file = path.with_suffix(".key")
    if key_file.exists():
        raw = bytes.fromhex(key_file.read_text().strip())
        return raw[:32], raw[32:]
    priv, pub = generate_keypair()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text((priv + pub).hex())
    key_file.chmod(0o600)
    return priv, pub


def hash_game_spec(game_spec) -> str:
    """Hash of outcome-affecting fields only. Schedule params excluded so run-frequency
    changes don't invalidate in-flight bets."""
    canonical = {
        "game_id": game_spec.game_id,
        "name": game_spec.name,
        "description": game_spec.description,
        "rules": game_spec.rules,
        "outcomes": [
            {
                "condition": o.condition,
                "win_probability": o.win_probability,
                "payout_multiplier": o.payout_multiplier,
            }
            for o in game_spec.outcomes
        ],
        "min_bet": game_spec.min_bet,
        "max_bet": game_spec.max_bet,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
