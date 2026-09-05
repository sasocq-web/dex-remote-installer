# Histórico de versões

## 1.0.0+20260905.145536 — 2026-09-05

- Sincroniza integralmente o runtime do Dex ativo de 05/09/2026.
- Inclui Workbench, Automações, Sites e insights do PC.
- Fixa o Codex CLI em 0.153.4 para reinstalações reproduzíveis.
- Adiciona reinstalador de um comando, restauração privada opcional e bundle
  local preservado pelo backup criptografado.
- Acrescenta o perfil `sasocq`, com o pacote fixado do Control Plane para
  recuperar broker, KVM, recursos, backups, sessões e Steam.
- Corrige nomes de assets com `+` codificado nas URLs de download do GitHub.
- Torna o manifesto do bundle determinístico para que a verificação pós-backup
  não gere uma falsa alteração no conteúdo recuperável.
- Aceita identificadores de release descritivos, sem pressupor um epoch no
  último segmento do nome.

## 1.0.0+20260822.193121 — 2026-08-22

- Sincronização com `automatic-approval-silent224`, incluindo as atualizações
  atuais do Dex e preservando as adaptações portáveis.

## 1.0.0+20260822.223 — 2026-08-22

- Primeira distribuição pública em formato `.deb`.
- Perfis independentes **Projetos** e **Sistema + Projetos**.
- Instalação do Codex oficial por usuário, sem incluir contas ou credenciais.
- Testes estruturais, de empacotamento e de execução HTTP dos dois perfis.
