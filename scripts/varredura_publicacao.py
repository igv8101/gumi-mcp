"""Ultimo portao antes de publicar: nada pessoal pode estar rastreado no git.

Roda sobre o que o git REALMENTE vai enviar (git ls-files), nao sobre a pasta:
arquivo ignorado nao importa, arquivo rastreado importa muito. Sai com codigo 1
se achar qualquer coisa - serve para rodar na mao ou num hook de pre-push.

    .venv\\Scripts\\python.exe scripts\\varredura_publicacao.py

Os padroes daqui sao genericos de proposito. Uma lista de termos ESPECIFICOS
(nomes de arquivo de dados, nome do dono) revelaria justamente o que se quer
esconder - entao ela mora em `termos.local.txt`, um por linha, fora do git.
"""

import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Vazamentos que qualquer repositorio pode ter, e que nao revelam nada ao serem
# nomeados aqui.
PROIBIDOS = [
    (r"C:\\Users\\[A-Za-z0-9_.-]+", "caminho absoluto de uma maquina Windows"),
    (r"/home/[a-z][A-Za-z0-9_.-]*", "caminho absoluto de uma maquina Unix"),
    (r"/Users/[A-Za-z0-9_.-]+", "caminho absoluto de um Mac"),
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "endereco de e-mail"),
    (r"(?i)\b(api[_-]?key|secret|password|senha|token)\s*[:=]\s*['\"][^'\"]{6,}", "credencial literal"),
    (r"ghp_[A-Za-z0-9]{20,}", "token do GitHub"),
    (r"sk-[A-Za-z0-9]{20,}", "chave de API"),
]

# Arquivos internos que nunca devem ser versionados, mesmo vazios.
ARQUIVOS_PROIBIDOS = (
    "privado.local.json",
    "alvos.local.json",
    "termos.local.txt",
    "ESTADO.md",
    "CLAUDE.md",
)

PREFIXOS_PROIBIDOS = ("ANALISE_E_ARQUITETURA",)


def _termos_locais() -> list[tuple[str, str]]:
    """Termos especificos deste projeto, um por linha. Linhas com # sao nota."""
    arquivo = RAIZ / "termos.local.txt"
    try:
        linhas = arquivo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [
        (re.escape(linha.strip()), "termo privado deste projeto")
        for linha in linhas
        if linha.strip() and not linha.strip().startswith("#")
    ]


def rastreados() -> list[str]:
    saida = subprocess.run(
        ["git", "ls-files"],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8",
    )
    return [linha for linha in saida.stdout.splitlines() if linha.strip()]


def principal() -> int:
    padroes = PROIBIDOS + _termos_locais()
    locais = len(padroes) - len(PROIBIDOS)
    arquivos = rastreados()

    if not arquivos:
        print("nada rastreado ainda - rode git add antes")
        return 1

    achados = []
    eu_mesmo = "scripts/varredura_publicacao.py"

    for relativo in arquivos:
        nome = relativo.rsplit("/", 1)[-1]
        if nome in ARQUIVOS_PROIBIDOS or relativo.startswith(PREFIXOS_PROIBIDOS):
            achados.append((relativo, 0, f"arquivo interno versionado ({nome})"))
            continue

        if relativo == eu_mesmo:
            continue  # os padroes daqui casariam consigo mesmos

        try:
            texto = (RAIZ / relativo).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for numero, linha in enumerate(texto.splitlines(), start=1):
            for padrao, porque in padroes:
                if re.search(padrao, linha):
                    achados.append((relativo, numero, f"{porque}: {linha.strip()[:70]}"))

    print(f"arquivos rastreados: {len(arquivos)}")
    print(f"padroes: {len(PROIBIDOS)} genericos + {locais} locais")
    print()

    if achados:
        print(f"REPROVADO - {len(achados)} ocorrencia(s):")
        for relativo, numero, motivo in achados[:40]:
            print(f"  {relativo}:{numero}  {motivo}")
        if len(achados) > 40:
            print(f"  ... e mais {len(achados) - 40}")
        return 1

    print("APROVADO: nada pessoal nos arquivos que o git vai enviar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(principal())
