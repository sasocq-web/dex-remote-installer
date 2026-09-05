# Atualização automática

No servidor de origem, `scripts/publish-after-backup` é executado por um timer
a cada três horas, depois da atualização automática do Codex CLI e antes e
depois do backup diário. O processo:

1. identifica a última release preparada ou ativa do Dex;
2. faz uma mesclagem de três vias com a base anteriormente publicada;
3. preserva as adaptações portáveis do instalador;
4. compila, empacota, verifica credenciais e testa os três perfis;
5. envia o commit e cria uma GitHub Release com o `.deb` e seu SHA-256.
6. gera o mesmo artefato em `recovery/`, acompanhado de manifesto e
   reinstalador;
7. quando o bundle muda fora da janela diária, solicita imediatamente um novo
   backup pelo `sasocq-brokerctl` e mantém um marcador até o backup confirmar.

O bundle local é montado a partir dos arquivos baixados novamente da GitHub
Release, e não de uma segunda compilação. Assim, GitHub e backup preservam
exatamente os mesmos bytes e o mesmo SHA-256.

O hook anterior ao backup fecha a janela em que uma release descoberta depois
do snapshot ficaria de fora. O hook posterior confirma a inclusão e remove o
marcador de pendência. Se a mesma release já estiver publicada, a rotina
termina sem criar commits vazios. Conflitos, ausência de autenticação ou testes
com falha impedem a publicação, mas não transformam um backup válido em falha.

As credenciais do GitHub pertencem à conta de serviço local que executa o
processo e ficam no armazenamento protegido do cliente `gh`; não fazem parte
do repositório, dos logs ou do pacote.

O pacote fixa a versão do Codex CLI usada pela release. Dados privados nunca
entram no GitHub; o comando `dex-remote-restore` os importa apenas de um
snapshot explicitamente indicado pelo operador.
