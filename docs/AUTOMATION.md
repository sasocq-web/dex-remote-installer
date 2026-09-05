# Atualização automática

No servidor de origem, `scripts/publish-after-backup` é executado somente após
o backup diário terminar com sucesso. O processo:

1. identifica a última release preparada ou ativa do Dex;
2. faz uma mesclagem de três vias com a base anteriormente publicada;
3. preserva as adaptações portáveis do instalador;
4. compila, empacota, verifica credenciais e testa os dois perfis;
5. envia o commit e cria uma GitHub Release com o `.deb` e seu SHA-256.
6. permite gerar o mesmo artefato em `recovery/` para o próximo backup
   criptografado, acompanhado de manifesto e reinstalador.

Se a mesma release já estiver publicada, a rotina termina sem criar commits
vazios. Conflitos, ausência de autenticação ou testes com falha impedem a
publicação, mas não transformam um backup válido em falha.

As credenciais do GitHub pertencem à conta de serviço local que executa o
processo e ficam no armazenamento protegido do cliente `gh`; não fazem parte
do repositório, dos logs ou do pacote.

O pacote fixa a versão do Codex CLI usada pela release. Dados privados nunca
entram no GitHub; o comando `dex-remote-restore` os importa apenas de um
snapshot explicitamente indicado pelo operador.
