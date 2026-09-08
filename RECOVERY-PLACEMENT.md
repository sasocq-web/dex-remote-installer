# Recuperação com a VM exclusiva para produção

A partir de `1.0.0+20260908.120700` e Control Plane `0.4.0+ubuntu26.04.28`, o perfil SASOCQ inclui obrigatoriamente a política `host-development-v1`.

O instalador reinstala os helpers e limites do laboratório no host e reaplica a regra às instruções de Sistema e Projetos. A importação de um backup antigo preserva os arquivos, mas reaplica o bloco atual por último. A criação de uma VM nova inclui os bloqueios no cloud-init; a recuperação de um disco antigo aplica os bloqueios ao disco desligado antes de iniciá-lo. Falhas impedem considerar a recuperação concluída. Não há alternativa automática que transfira desenvolvimento para a VM.

Projetos não recebe sudo, broker nem libvirt do host. Os builds usam containers rootless e o Studio usa o laboratório isolado. A VM recebe artefatos prontos e mantém sites, bancos, serviços e seus processamentos em `sasocq.com`. Os limites dos laboratórios são agregados e o hardware físico continua compartilhado.

Use o bundle atual, verifique seus checksums e execute `reinstall --mode sasocq` somente no ambiente de recuperação ou numa instalação nova. O reinstalador recusa execução sobre o Dex ativo. Para recuperar o estado privado, utilize `--restore-from <snapshot-local-descriptografado>`. O sistema preserva a configuração do novo instalador e reintroduz a política depois dos arquivos antigos.

Os programas proprietários, perfis autenticados, projetos e dados não acompanham o pacote público. Restaure-os do backup criptografado, incluindo `/srv/sasocq/projects`, `/srv/sasocq/development`, `/srv/sasocq/lab` e `/var/lib/sasocq-lab`. Se um runtime faltar, o Sistema deverá prepará-lo no host; não instalar na VM. A imagem SASOCQ gerencia `codex-worker` com UID 1001 em x86_64. Conflitos de identidade/arquitetura exigem reparo explícito, sem renumerar contas ou reduzir o isolamento automaticamente.

Valide depois da recuperação:

- `python3 -m sasocq_control.placement_policy check` no host, pelo Sistema; repetir como `codex-worker`.
- `docker build .` na VM deve recusar a operação; Docker de produção permanece disponível.
- Serviços Android antigos da VM devem permanecer mascarados; a política AppArmor do Studio deve estar carregada.
- Abrir e testar o laboratório necessário no host, encerrando-o depois; confirmar a produção pelos endereços reais.

Mídias antigas e uma instalação genérica do Ubuntu não contêm esta política por si sós. Use o instalador/bundle atualizado. Isto não retira os poderes de um administrador root nem representa proteção absoluta contra vulnerabilidades do kernel. As restrições operacionais também permanecem nas instruções sincronizadas das duas identidades.

Ao atualizar helpers do laboratório, atualize também `vendor/placement/payload/manifest.json` e o pacote do Control Plane. Os hashes são verificados antes da instalação e do início de novos workers SASOCQ; não enfraqueça a verificação para aceitar divergências.
