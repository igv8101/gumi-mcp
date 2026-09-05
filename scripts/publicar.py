"""Publica o Gumi-MCP no GitHub, com os portoes de seguranca antes.

Rode DEPOIS de `gh auth login`. Ele descobre seu usuario, ajusta README e
LICENSE com ele, roda a varredura de dados pessoais e a suite, mostra tudo o
que vai fazer e SO ENTAO pergunta se pode criar o repositorio.

    .venv\\Scripts\\python.exe scripts\\publicar.py

Nada e enviado sem voce digitar "sim". Se qualquer portao reprovar, o script
para e nao publica.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
NOME_REPO = "gumi-mcp"
DESCRICAO = (
    "Servidor MCP local, somente leitura, que expoe um repositorio a agentes "
    "de IA sem expor os dados que vivem dentro dele"
)

GH = "gh"
for candidato in (r"C:\Program Files\GitHub CLI\gh.exe", r"C:\Program Files (x86)\GitHub CLI\gh.exe"):
    if Path(candidato).exists():
        GH = candidato
        break


def rodar(comando, **kwargs):
    return subprocess.run(
        comando, cwd=RAIZ, capture_output=True, text=True,
        encoding="utf-8", errors="replace", **kwargs,
    )


def passo(numero, texto):
    print()
    print(f"[{numero}] {texto}")


# ============================================================

def quem_sou_eu() -> tuple[str, str, str] | None:
    """Devolve (usuario, nome, email_de_commit).

    O email do commit fica gravado no historico PARA SEMPRE e e publico, entao
    aqui se usa o endereco noreply do GitHub em vez de um e-mail real.

    Detalhe que passa batido: em contas criadas depois de 18/07/2017 o noreply
    precisa do ID numerico na frente (`ID+usuario@users.noreply.github.com`).
    Sem o ID, o GitHub nao liga os commits ao perfil - e o grafo de
    contribuicoes fica vazio, que e justamente o que se quer mostrar.
    """
    resultado = rodar([GH, "api", "user"])
    if resultado.returncode != 0:
        return None
    try:
        dados = json.loads(resultado.stdout)
    except json.JSONDecodeError:
        return None

    usuario = dados.get("login")
    nome = dados.get("name") or usuario
    identificador = dados.get("id")

    if identificador:
        email = f"{identificador}+{usuario}@users.noreply.github.com"
    else:
        email = f"{usuario}@users.noreply.github.com"

    return usuario, nome, email


def ajustar_identidade(usuario: str, nome: str) -> list[str]:
    """Troca o placeholder do README e o titular do LICENSE."""
    mudou = []

    readme = RAIZ / "README.md"
    texto = readme.read_text(encoding="utf-8")
    novo = re.sub(r"github\.com/[A-Za-z0-9_-]+/gumi-mcp", f"github.com/{usuario}/gumi-mcp", texto)
    if novo != texto:
        readme.write_text(novo, encoding="utf-8")
        mudou.append(f"README.md -> github.com/{usuario}/gumi-mcp")

    licenca = RAIZ / "LICENSE"
    texto = licenca.read_text(encoding="utf-8")
    novo = re.sub(r"Copyright \(c\) 2026 .+", f"Copyright (c) 2026 {nome}", texto)
    if novo != texto:
        licenca.write_text(novo, encoding="utf-8")
        mudou.append(f"LICENSE -> {nome}")

    return mudou


def principal() -> int:
    print("Publicacao do Gumi-MCP")
    print("=" * 60)

    passo(1, "Conferindo o login do GitHub")
    identidade = quem_sou_eu()
    if not identidade:
        print("    NAO AUTENTICADO. Rode primeiro:  gh auth login")
        return 1
    usuario, nome, email_commit = identidade
    print(f"    logado como {usuario} ({nome})")
    print(f"    e-mail dos commits: {email_commit}")

    passo(2, "Ajustando README e LICENSE com o seu nome")
    for linha in ajustar_identidade(usuario, nome) or ["nada a mudar"]:
        print(f"    {linha}")

    passo(3, "Varredura de dados pessoais (o que o git vai enviar)")
    rodar(["git", "add", "-A"])
    varredura = rodar([sys.executable, "scripts/varredura_publicacao.py"])
    print("   ", varredura.stdout.strip().replace("\n", "\n    "))
    if varredura.returncode != 0:
        print("    REPROVADO - nada foi publicado.")
        return 1

    passo(4, "Rodando a suite")
    testes = rodar([sys.executable, "-m", "pytest", "-q"])
    ultima = [l for l in testes.stdout.strip().splitlines() if l.strip()][-1:]
    print(f"    {ultima[0] if ultima else 'sem saida'}")
    if testes.returncode != 0:
        print("    TESTE VERMELHO - nada foi publicado.")
        return 1

    passo(5, "Verificacao adversarial")
    adversarial = rodar([sys.executable, "tests/verificacao_adversarial.py"])
    veredito = [l for l in adversarial.stdout.splitlines() if "APROVADO" in l or "REPROVADO" in l]
    print(f"    {veredito[0].strip() if veredito else 'sem veredito'}")
    if adversarial.returncode != 0:
        print("    REPROVADO - nada foi publicado.")
        return 1

    passo(6, "Commitando os ajustes de identidade, se houver")
    rodar(["git", "add", "-A"])
    tem_mudanca = rodar(["git", "diff", "--cached", "--quiet"]).returncode != 0
    if tem_mudanca:
        rodar([
            "git", "-c", f"user.name={nome}", "-c", f"user.email={email_commit}",
            "commit", "-q", "-m", "Ajusta identidade do repositorio",
        ])
        # Fixa a identidade NESTE repo, para os proximos commits tambem
        # sairem com o noreply e serem atribuidos ao perfil.
        rodar(["git", "config", "user.name", nome])
        rodar(["git", "config", "user.email", email_commit])
        print("    commitado")
    else:
        print("    nada a commitar")

    arquivos = rodar(["git", "ls-files"]).stdout.strip().splitlines()

    print()
    print("=" * 60)
    print("TUDO PRONTO. O que vai acontecer:")
    print(f"  - criar https://github.com/{usuario}/{NOME_REPO}  (PUBLICO)")
    print(f"  - enviar {len(arquivos)} arquivos, 1 commit")
    print("  - o historico antigo NAO vai (esta fora da pasta)")
    print("=" * 60)
    resposta = input('Digite "sim" para publicar: ').strip().lower()

    if resposta != "sim":
        print("Cancelado. Nada foi enviado.")
        return 0

    passo(7, "Criando o repositorio e enviando")
    criar = rodar([
        GH, "repo", "create", NOME_REPO,
        "--public", "--source=.", "--push",
        "--description", DESCRICAO,
    ])
    saida = (criar.stdout + criar.stderr).strip()
    print("   ", saida.replace("\n", "\n    "))

    if criar.returncode != 0:
        print()
        print("    Falhou. O repositorio pode ja existir - nesse caso rode:")
        print(f"      git remote add origin https://github.com/{usuario}/{NOME_REPO}.git")
        print("      git push -u origin main")
        return 1

    url = f"https://github.com/{usuario}/{NOME_REPO}"
    print()
    print(f"PUBLICADO: {url}")
    print()
    print("Ultimos toques que valem 2 minutos, no site:")
    print("  - adicione os topics: mcp, model-context-protocol, python, privacy, security")
    print("  - confira a aba Actions: a CI deve rodar sozinha e ficar verde")
    print(f"  - fixe o repo no seu perfil: {url}  ->  aba do perfil, 'Customize your pins'")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(principal())
    except KeyboardInterrupt:
        print("\nCancelado.")
        raise SystemExit(1)
