"""Diagnóstico da política: o que ficou visível e o que ficou de fora, e por quê.

Serve para calibrar. Privacidade que esconde o código do projeto atrapalha o
objetivo — este script mostra se a régua está no lugar certo.
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402

from config import GUMI_ROOT, IGNORE_DIRECTORIES  # noqa: E402
from security import esta_ignorado, motivo_privacidade  # noqa: E402


def principal() -> None:
    visiveis = Counter()
    privados = Counter()
    extensoes_visiveis = Counter()
    total_disco = 0

    for diretorio, subdiretorios, arquivos in os.walk(GUMI_ROOT, topdown=True):
        base = Path(diretorio)
        subdiretorios[:] = [
            nome for nome in subdiretorios
            if nome.casefold() not in IGNORE_DIRECTORIES
        ]
        for nome in arquivos:
            caminho = base / nome
            total_disco += 1

            if esta_ignorado(caminho):
                continue

            relativo = caminho.relative_to(GUMI_ROOT)
            topo = relativo.parts[0] if len(relativo.parts) > 1 else "(raiz)"

            if motivo_privacidade(caminho) is None:
                visiveis[topo] += 1
                extensoes_visiveis[caminho.suffix.casefold() or "(sem)"] += 1
            else:
                privados[topo] += 1

    print(f"arquivos no disco (fora de pastas geradas): {total_disco}")
    print(f"visiveis: {sum(visiveis.values())}   privados: {sum(privados.values())}")
    print()
    print("--- VISIVEIS por pasta de topo ---")
    for pasta, quantidade in visiveis.most_common(20):
        print(f"  {quantidade:6}  {pasta}")
    print()
    print("--- PRIVADOS por pasta de topo ---")
    for pasta, quantidade in privados.most_common(20):
        print(f"  {quantidade:6}  {pasta}")
    print()
    print("--- extensoes visiveis ---")
    for extensao, quantidade in extensoes_visiveis.most_common(15):
        print(f"  {quantidade:6}  {extensao}")


if __name__ == "__main__":
    principal()
