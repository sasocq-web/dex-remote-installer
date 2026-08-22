# Dex Remote Installer

Instalador Debian/Ubuntu reutilizável e sem dados pessoais para executar dois
Codex remotos em qualquer computador Linux compatível. O `.deb` oferece dois
perfis independentes:

- `projects`: somente o Codex de Projetos, executado como `codex-worker`, sem sudo;
- `full`: Codex do Sistema como `codex`, com `sudo NOPASSWD: ALL`, e Codex de
  Projetos separado como `codex-worker`.

O pacote não contém tokens OpenAI, cookies, conversas, chaves SSH, contas de
nuvem, identificadores OAuth ou configurações do servidor de origem. Cada conta
é autenticada pelo proprietário depois da instalação.

## Compatibilidade

Um `.deb` é instalável nativamente em Debian, Ubuntu e derivados com systemd.
O conteúdo é `Architecture: all`; o instalador oficial do Codex seleciona o
binário compatível com a arquitetura do computador. Distribuições RPM, Arch e
outras famílias precisam de outro formato de pacote.

## Instalação

```bash
sudo apt install ./dex-remote-installer_*_all.deb
```

O Debconf pergunta qual perfil instalar. Para reconfigurar depois:

```bash
sudo dex-remote-setup --mode projects --install-codex
sudo dex-remote-setup --mode full --install-codex
```

Abra `http://127.0.0.1:8787` ou o ícone **Dex Remoto** e autentique a conta
OpenAI. No perfil completo, entre no Codex do Sistema e aplique a mesma conta ao
Codex de Projetos pela interface, ou autentique cada identidade separadamente.

Para acesso remoto privado por Tailscale:

```bash
sudo dex-remote-setup --mode projects --skip-codex --enable-tailscale
```

## Segurança

O backend escuta somente em loopback. O localhost permanece autorizado para o
primeiro acesso e para recuperação; acessos remotos continuam exigindo um
navegador pareado. O perfil completo é deliberadamente
poderoso: qualquer processo executado como `codex` pode usar sudo sem senha.
O perfil de projetos não recebe sudo. A remoção do pacote preserva as contas e
homes para evitar perda automática de projetos, conversas e autenticações.

## Verificação do download

Baixe o `.deb` e o arquivo `.sha256` da GitHub Release mais recente e execute:

```bash
sha256sum --check dex-remote-installer_*.deb.sha256
```

## Atualizações e desenvolvimento

O código-fonte, o histórico e os artefatos de cada versão são publicados no
GitHub. No servidor mantenedor, uma rotina executada depois de cada backup
diário verifica se existe uma release nova do Dex. Ela só publica um novo
pacote depois de mesclar as adaptações portáveis e concluir os testes dos dois
perfis; falhas ou conflitos nunca substituem a última release válida.

Para compilar e testar localmente:

```bash
./build.sh
./tests/test-package.sh
```

Veja [docs/AUTOMATION.md](docs/AUTOMATION.md) para o fluxo de atualização e
[SECURITY.md](SECURITY.md) para relatar vulnerabilidades.

## Licença

MIT. Consulte [LICENSE](LICENSE).
