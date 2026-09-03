"""Cálculo de hash do Gumi-MCP — um lugar só (L2).

Antes o SHA-256 vivia duplicado em `server.py` e `watcher.py`, com regras
diferentes para arquivo grande e para erro de leitura. Agora a regra é uma:
acima do teto ou com erro de leitura, o hash é None — nunca uma exceção.
"""

import hashlib
from pathlib import Path

from config import MAX_FILE_SIZE


def sha256_do_arquivo(path: Path, limite: int | None = None) -> str | None:
    """SHA-256 em blocos; None acima do teto ou em erro de leitura (F7)."""
    teto = MAX_FILE_SIZE if limite is None else limite

    try:
        if path.stat().st_size > teto:
            return None

        sha = hashlib.sha256()
        with path.open("rb") as arquivo:
            for bloco in iter(lambda: arquivo.read(1024 * 1024), b""):
                sha.update(bloco)
        return sha.hexdigest()

    except OSError:
        return None
