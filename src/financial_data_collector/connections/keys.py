"""Where a connection's key is kept.

In the operating system's key store (Windows Credential Manager, the macOS Keychain, the Linux Secret Service)
through keyring: one entry per connection per warehouse, named by a random reference that the connections table
holds. On a computer with no key store, in .env beside config.toml, written by fdc connect. Never anywhere else.
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol
from urllib.parse import unquote, urlsplit

from dotenv import dotenv_values

SERVICE = "financial-data-collector"
ENV_REF = "env"  # the key_ref of a connection whose key lives in .env
FIELDS = {"snaptrade": ("client_id", "consumer_key", "user_id", "user_secret"), "simplefin": ("access_url",)}
REQUIRED = {"snaptrade": ("client_id", "consumer_key"), "simplefin": ("access_url",)}


def env_name(connection: str, field: str) -> str:
    return f"FDC_{connection}_{field}".upper()


@dataclass(frozen=True)
class SnapTradeKeys:
    client_id: str
    consumer_key: str
    user_id: str | None = None      # only for a key that has a registered user; a personal key has none
    user_secret: str | None = None

    def values(self) -> dict[str, str]:
        pairs = (("client_id", self.client_id), ("consumer_key", self.consumer_key),
                 ("user_id", self.user_id), ("user_secret", self.user_secret))
        return {k: v for k, v in pairs if v}

    def secrets(self) -> list[str]:
        return list(self.values().values())


@dataclass(frozen=True)
class SimpleFinKey:
    access_url: str  # https://user:password@host/path: the whole address is the secret

    def values(self) -> dict[str, str]:
        return {"access_url": self.access_url}

    def secrets(self) -> list[str]:
        parts = urlsplit(self.access_url)
        raw = [parts.username or "", parts.password or ""]
        return [s for s in [self.access_url, *raw, *(unquote(x) for x in raw)] if s]


Keys = SnapTradeKeys | SimpleFinKey


def parse(connection: str, values: Mapping[str, str | None]) -> Keys | None:
    """The keys in a saved or environment mapping, stripped; None when a required value is missing or blank."""
    clean = {k: str(values.get(k) or "").strip() for k in FIELDS.get(connection, ())}
    if connection not in REQUIRED or any(not clean[k] for k in REQUIRED[connection]):
        return None
    if connection == "snaptrade":
        return SnapTradeKeys(clean["client_id"], clean["consumer_key"], clean["user_id"] or None,
                             clean["user_secret"] or None)
    return SimpleFinKey(clean["access_url"])


class NoKeyStore(Exception):
    """This computer has no key store a program can use."""


class KeyStore(Protocol):
    def get(self, ref: str) -> str | None: ...
    def set(self, ref: str, secret: str) -> None: ...
    def delete(self, ref: str) -> None: ...


class OsKeyStore:
    """The operating system's key store, through keyring. Every failure becomes NoKeyStore, named by its type
    only: keyring's own messages say nothing a person can act on, and none of them belongs in a log."""

    def __init__(self, backend=None):
        self._backend = backend

    def _keyring(self):
        if self._backend is None:
            try:
                import keyring
            except ImportError:
                raise NoKeyStore("keyring is not installed") from None
            self._backend = keyring
        return self._backend

    def get(self, ref: str) -> str | None:
        try:
            return self._keyring().get_password(SERVICE, ref)
        except NoKeyStore:
            raise
        except Exception as e:
            raise NoKeyStore(type(e).__name__) from None

    def set(self, ref: str, secret: str) -> None:
        try:
            self._keyring().set_password(SERVICE, ref, secret)
        except NoKeyStore:
            raise
        except Exception as e:
            raise NoKeyStore(type(e).__name__) from None

    def delete(self, ref: str) -> None:
        try:
            self._keyring().delete_password(SERVICE, ref)
        except Exception:
            pass  # gone already, or no key store: either way nothing is left to delete


def os_key_store() -> KeyStore:
    """The key store KeyHome.for_root uses. Tests replace this one name, so the class itself stays testable."""
    return OsKeyStore()


class MemoryKeyStore:
    """For tests, and for the fixture that keeps every test away from the real key store."""

    def __init__(self, broken: bool = False):
        self.entries: dict[str, str] = {}
        self.broken = broken

    def get(self, ref: str) -> str | None:
        if self.broken:
            raise NoKeyStore("broken")
        return self.entries.get(ref)

    def set(self, ref: str, secret: str) -> None:
        if self.broken:
            raise NoKeyStore("broken")
        self.entries[ref] = secret

    def delete(self, ref: str) -> None:
        self.entries.pop(ref, None)


class EnvFile:
    """The FDC_* variables: the environment first, then .env. Written only when there is no key store."""

    def __init__(self, path: Path, environ: Mapping[str, str] | None = None):
        self.path = Path(path)
        self.environ: Mapping[str, str] = os.environ if environ is None else environ

    def _marker(self, connection: str) -> str:
        return f"# {connection} connection, written by fdc connect (no key store on this computer)"

    def _file_values(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        return {k: v for k, v in dotenv_values(self.path).items() if v}

    def read(self, connection: str) -> dict[str, str]:
        file_values = self._file_values()
        out: dict[str, str] = {}
        for field in FIELDS.get(connection, ()):
            name = env_name(connection, field)
            value = str(self.environ.get(name) or file_values.get(name) or "").strip()
            if value:
                out[field] = value
        return out

    def write(self, connection: str, values: Mapping[str, str]) -> None:
        names = {env_name(connection, f): values.get(f, "") for f in FIELDS.get(connection, ())}
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.is_file() else []
        kept = [ln for ln in lines if ln.split("=", 1)[0].strip() not in names and ln != self._marker(connection)]
        kept.append(self._marker(connection))
        kept += [f"{name}={value}" for name, value in names.items() if value]
        self.path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    def remove(self, connection: str) -> None:
        if not self.path.is_file():
            return
        names = {env_name(connection, f) for f in FIELDS.get(connection, ())}
        lines = self.path.read_text(encoding="utf-8").splitlines()
        kept = [ln for ln in lines if ln.split("=", 1)[0].strip() not in names and ln != self._marker(connection)]
        self.path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


class KeyHome:
    """Saves, loads and forgets a connection's key: the key store when it works, .env when it doesn't."""

    def __init__(self, store: KeyStore | None, env: EnvFile):
        self.store = store
        self.env = env

    @classmethod
    def for_root(cls, root: Path) -> "KeyHome":
        return cls(os_key_store(), EnvFile(Path(root) / ".env"))

    def save(self, connection: str, keys: Keys) -> str:
        """Returns the key_ref for the connections table: a random reference, or ENV_REF when .env had to be used."""
        if self.store is not None:
            ref = secrets.token_hex(16)
            try:
                self.store.set(ref, json.dumps(keys.values()))
                return ref
            except NoKeyStore:
                pass
        self.env.write(connection, keys.values())
        return ENV_REF

    def load(self, connection: str, key_ref: str) -> Keys | None:
        if key_ref != ENV_REF and self.store is not None:
            try:
                raw = self.store.get(key_ref)
            except NoKeyStore:
                raw = None
            if raw:
                try:
                    return parse(connection, json.loads(raw))
                except ValueError:
                    return None
        return self.in_env(connection)

    def forget(self, connection: str, key_ref: str) -> None:
        if key_ref == ENV_REF:
            self.env.remove(connection)
        elif self.store is not None:
            self.store.delete(key_ref)

    def in_env(self, connection: str) -> Keys | None:
        return parse(connection, self.env.read(connection))
