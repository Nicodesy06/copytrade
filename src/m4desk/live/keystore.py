"""Chiave di firma nel Portachiavi di macOS (libreria `keyring`, backend macOS esplicito).

La chiave non viene mai scritta su file, log, DB, prompt LLM o dashboard. Si importa solo da input
nascosto (CLI `getpass` o campo protetto dell'app) e si legge solo dentro `m4desk.live`.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from solders.keypair import Keypair

SERVICE = "M4 CopyDesk live signer"
ACCOUNT = "solana-signer"
log = logging.getLogger(__name__)


class KeyStoreError(Exception):
    pass


def default_backend() -> Any:
    """Backend esplicito: nelle app congelate la scoperta automatica dei backend può fallire."""
    if sys.platform == "darwin":
        from keyring.backends import macOS

        return macOS.Keyring()
    import keyring

    return keyring.get_keyring()


def parse_secret(text: str) -> Keypair:
    """Accetta: base58 (64 byte chiave completa o 32 byte seed) oppure array JSON di 64 interi."""
    t = (text or "").strip()
    if not t:
        raise KeyStoreError("chiave vuota")
    if t.split()[0].isalpha() and len(t.split()) >= 12:
        raise KeyStoreError("sembra una seed phrase: non è supportata, esporta la CHIAVE PRIVATA")
    try:
        if t.startswith("["):
            raw = bytes(json.loads(t))
            if len(raw) != 64:
                raise KeyStoreError("array JSON: servono 64 byte")
            return Keypair.from_bytes(raw)
        raw = _b58decode(t)
    except KeyStoreError:
        raise
    except Exception as e:
        raise KeyStoreError("formato non riconosciuto") from e
    if len(raw) == 64:
        try:
            kp = Keypair.from_bytes(raw)
        except ValueError as e:
            raise KeyStoreError("chiave incoerente: la parte pubblica non corrisponde") from e
        if bytes(kp.pubkey()) != raw[32:]:
            raise KeyStoreError("chiave incoerente: la parte pubblica non corrisponde")
        return kp
    if len(raw) == 32:
        return Keypair.from_seed(raw)
    raise KeyStoreError(f"lunghezza non valida ({len(raw)} byte)")


_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        if ch not in _ALPHABET:
            raise KeyStoreError("carattere non base58")
        n = n * 58 + _ALPHABET.index(ch)
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + full


def _b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _ALPHABET[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


class KeyStore:
    def __init__(self, backend: Any | None = None):
        self._backend = backend

    @property
    def backend(self) -> Any:
        if self._backend is None:
            self._backend = default_backend()
        return self._backend

    def available(self) -> tuple[bool, str]:
        try:
            b = self.backend
        except Exception as e:
            return False, f"Portachiavi non disponibile ({type(e).__name__})"
        mod = type(b).__module__
        if mod.endswith(".fail") or mod.endswith(".null") or "plaintext" in mod.lower():
            return False, "nessun portachiavi sicuro disponibile su questo sistema"
        return True, f"{mod}.{type(b).__name__}"

    def store(self, secret_text: str) -> str:
        """Valida, salva nel Portachiavi e ritorna SOLO l'indirizzo pubblico."""
        kp = parse_secret(secret_text)
        self.backend.set_password(SERVICE, ACCOUNT, _b58encode(bytes(kp)))
        return str(kp.pubkey())

    def has_key(self) -> bool:
        try:
            return self.backend.get_password(SERVICE, ACCOUNT) is not None
        except Exception:
            return False

    def pubkey(self) -> str | None:
        kp = self._load()
        return str(kp.pubkey()) if kp else None

    def _load(self) -> Keypair | None:
        v = self.backend.get_password(SERVICE, ACCOUNT)
        return Keypair.from_bytes(_b58decode(v)) if v else None

    def signer(self) -> Keypair:
        kp = self._load()
        if kp is None:
            raise KeyStoreError("nessuna chiave nel Portachiavi: `m4desk live import-key`")
        return kp

    def forget(self) -> None:
        try:
            self.backend.delete_password(SERVICE, ACCOUNT)
        except Exception as e:  # già assente
            log.info("nessuna chiave da cancellare (%s)", type(e).__name__)


class MemoryBackend:
    """Solo per i test."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def set_password(self, s: str, a: str, p: str) -> None:
        self.data[(s, a)] = p

    def get_password(self, s: str, a: str) -> str | None:
        return self.data.get((s, a))

    def delete_password(self, s: str, a: str) -> None:
        self.data.pop((s, a), None)
