"""Limpeza única: tira do índice o que as regras novas tornaram privado.

O índice foi construído antes da política de privacidade existir, então guarda
metadados (nome, tamanho, hash) de arquivos da zona íntima. Isto reconcilia o
banco com as regras atuais. Rodar uma vez, depois do L1.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


if __name__ == "__main__":
    print("antes: ", server.get_project_state())
    print("reconciliando...")
    print(server.reconcile_project_state())
    print("depois:", server.get_project_state())
