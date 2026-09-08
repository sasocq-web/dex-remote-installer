# Studio e laboratórios fora da VM — 8/9/2026

O Codex de Projetos, como `codex-worker` no host, dispõe de `sasocq-lab` sem sudo
do host. A VM `sasocq-server` é reservada a sites, bancos e processamento da
hospedagem em `sasocq.com`. Testes gráficos e runtimes ficam em
`/srv/sasocq/lab/<nome>`, com tela e rede próprias.

## Roblox pelo próprio projeto

Na pasta `/srv/sasocq/projects/Roblox` do host:

```sh
bash test-client/migrate-to-host.sh
bash test-client/start-reviewed.sh
sasocq-lab screenshot roblox
sasocq-lab exec roblox -- xdotool mousemove 186 68 click 1
sasocq-lab status roblox
# Pare Play e feche Studio normalmente; depois encerre os auxiliares:
bash test-client/stop.sh
```

A importação lê a cópia verificada no host, em
`/srv/sasocq/development/vm-import/sasocq-server`, sem depender da VM.
Recusa sobrescrever uma importação existente.
A área `roblox` já foi importada e testada. Para uma nova cópia:

```sh
sasocq-lab import-local minha-revisao --project Roblox --prefix wine-visual-review --place execution-preview.rbxlx
sasocq-lab roblox minha-revisao
sasocq-lab roblox minha-revisao --file /caminho/para/previamente-gerado.rbxlx
```

`--file` copia a prévia local para `inputs`, identificada por hash; nunca modifica
o arquivo original. Encerre o Studio anterior antes de abrir outro arquivo.
`--home` abre a página inicial. Preserve arquivos editados úteis no projeto e
nunca publique automaticamente prévias que usam dados temporários.

`start.sh` mantém a seleção `dist/Brotalume-studio-preview.rbxlx` e padroniza o
Wine no perfil já testado. `start-reviewed.sh`, `start-companion-review.sh` e
`start-defense-review.sh` selecionam suas próprias prévias na área `roblox`.
`start-visual.sh` e `start-art.sh` importam sob demanda suas configurações nas
áreas `roblox-visual` e `roblox-art`. As revisões específicas precisam de sua
validação de conteúdo habitual; a validação desta migração usou execution-preview.
Nenhum desses scripts inicia testes por SSH. Os helpers antigos foram arquivados no host e recusam execução fora do laboratório.

## Isolamento e limites

- Bubblewrap sem privilégios, namespaces de processos/rede/mounts/IPC,
  capabilities vazias e NoNewPrivs. Kernel compartilhado, não outra VM.
- Sem homes/credenciais SSH ou Codex, broker, libvirt, D-Bus ou vídeo físico
  do host. Só a pasta da área é gravável persistentemente; `/usr` é somente leitura.
- Limites agregados pelo cgroup: MemoryHigh 4 GiB, MemoryMax 6 GiB, swap zero,
  CPUQuota 800%, CPUWeight/IOWeight 100, TasksMax 512. O host e a VM continuam
  compartilhando CPU, memória física e disco; isso limita a concorrência, não a elimina.
- Padrão 30 minutos, máximo 120, sem inicialização automática. `stop` encerra
  todos os descendentes, inclusive Wine, visor e rede; os arquivos permanecem.
- Disco: 4 GiB por arquivo; guarda monitorada de 40 GiB nas áreas e reserva
  de 50 GiB livres. A guarda agregada não é quota rígida de filesystem.
- Um laboratório do perfil Roblox por vez, com trava adicional contra duas
  aberturas de Studio na mesma área.
- Wine 10 extraído em runtime root-owned, sem instalar/atualizar Wine global.
  Tela virtual 1280×800 com Openbox e renderização por software; sem GPU física.
  O Play passou, mas houve avisos gráficos e isto não é qualificação de desempenho.

O perfil Roblox tem IPv4 virtual via slirp4netns, com DNS público e saída pública
TCP/UDP. Loopback do host e DNS do host ficam desabilitados. Um filtro root-owned
carregado no transporte slirp rejeita destinos privados/reservados e todo IPv6.
O supervisor verifica seu carregamento e só libera o ambiente após a rede estar
pronta. É filtragem no transporte em espaço de usuário, não firewall do kernel;
revalidar o filtro ao atualizar slirp/libslirp. Aplicações ficam em namespace
separado e não executam no namespace do transporte.

## Outros programas

```sh
sasocq-lab start meu-teste --gui --minutes 30
sasocq-lab exec meu-teste -- python3 -c 'print("teste no host")'
sasocq-lab launch meu-teste -- /work/programa
sasocq-lab screenshot meu-teste
sasocq-lab stop meu-teste
```

O perfil genérico é offline. `--internet` habilita proxy público HTTP CONNECT
(portas 80/443), sem acesso à VM/LAN; `--wine` monta o Wine isolado. Novos runtimes
devem ser preparados pelo Sistema quando as capacidades disponíveis não bastarem.
Nunca usar a VM da hospedagem como fallback. `--gpu` só aceita permissões já
existentes; este trabalho não concedeu GPU, KVM, sudo ou broker a Projetos.
Não foi acrescentado um visor ao menu web do Dex: o Codex usa screenshot e
controle auditado dentro da tela própria.

Android usa `android_control` sob demanda e deve ficar totalmente desligado ao
terminar. A mudança do ADB foi incorporada à release ativa
`20260908-queue-reorder304`. Abertura, leitura da interface, navegação, desligamento
e nova abertura foram validados pelo backend real da conversa, inclusive depois
da retirada dos componentes antigos da VM. ADB/Appium da VM estão mascarados;
perfis, ferramentas e unidades antigas foram arquivados no host e os pacotes
exclusivos de Android removidos da VM. Nenhuma ativação está pendente nesta migração.

## Proteção da VM e reversão

AppArmor na VM impede que o Wine padrão leia/execute `RobloxStudio*.exe`.
Não modifica a instalação Wine nem reinicia o MT5. Essa proteção evita o caminho
acidental conhecido, mas não limita um administrador com sudo capaz de removê-la.
As regras operacionais foram sincronizadas entre Sistema e Projetos: novos
testes pesados pertencem ao laboratório, não à VM.

Os processos antigos do Studio na VM foram encerrados. Os diretórios de
desenvolvimento selecionados foram transferidos por rsync com checksum e remoção
da origem somente após confirmação da cópia no host; backups existentes na VM
foram excluídos da remoção. O manifesto e os recibos ficam em
`/srv/sasocq/development/{migration-state,mixed-migration-state,relocation-state}.json`.
Configurações, segredos, bancos, imagens operacionais e seus mounts permanecem na VM. Backups dos helpers e scripts alterados estão em
`/var/backups/sasocq-lab304`; fontes e evidências em
`/home/codex/SystemWorkspace/host-lab304`. Reversão administrativa preserva as
áreas e não deve recolocar testes na VM sem nova decisão explícita do operador.

## Validação funcional

Importação realizada como `codex-worker`; Studio autenticado abriu a prévia,
Play atingiu `PlaySoloSuccess`, o caderno do jogo respondeu ao clique, Play foi
interrompido e Studio fechado pela interface. O script do projeto reabriu a
prévia no host. Verificações adicionais cobrem bloqueio à VM/rede privada,
negação de concorrência Roblox, limites reais e encerramento do laboratório.
Não equivale à validação do jogo publicado, de todos os perfis ou de Android.


## Código e builds de todos os projetos

O workspace atual de Projetos continua em `/srv/sasocq/projects`. Versões antigas
vindas da VM ficam separadas em `/srv/sasocq/development/vm-import/sasocq-server`,
com o caminho original abaixo dessa raiz, para não sobrescrever trabalho mais novo
no host. Selecione o conteúdo necessário e trabalhe no projeto; não execute
instaladores legados diretamente sobre o sistema principal.

```sh
sasocq-build build meu-app --context /srv/sasocq/projects/MeuApp --tag sasocq/meu-app:versao
sasocq-build status meu-app
sasocq-build run teste-app --image localhost/sasocq/meu-app:versao -- comando-de-teste
sasocq-build status teste-app
sasocq-build stop teste-app
```

Os comandos iniciam tarefas assíncronas em containers Podman rootless, sob os
mesmos limites agregados do laboratório. Não fornecem root, Docker socket,
credenciais SSH, GPU física, portas publicadas nem volumes do host ao container.
`run` usa somente imagens já disponíveis localmente. A rede dos comandos dentro
do container usa o transporte público filtrado; downloads de imagens-base são
feitos pelo gerenciador confiável no host. O contexto é copiado com exclusões de
credenciais em caminhos convencionais, `.env*`, caches e node_modules; revise
seu Dockerfile e contexto para não incluir segredos com outros nomes.

O kernel continua compartilhado. Esses limites protegem contra alterações comuns
no sistema e consumo excessivo, mas não equivalem a proteção absoluta contra
vulnerabilidades do kernel. O armazenamento de containers tem reserva monitorada
de 80 GiB livres no host, sem quota rígida. Não crie outro serviço Podman/Docker
para contornar os limites. Nenhum laboratório ou tarefa de build inicia no boot.

Após `phase=complete`, a imagem pronta está em
`/srv/sasocq/development/artifacts/<nome>.tar`. Transfira o artefato para a VM,
carregue com `docker load` e use `docker compose up -d --no-build`, preservando a
imagem anterior e os procedimentos normais de backup/rollback. Não faça deploy
apenas para testar uma construção. Valide cada aplicação antes de publicar e
valide o fluxo real após o deploy. A VM bloqueia os comandos usuais de build;
GCC/G++, build-essential e npm de desenvolvimento foram removidos, mantendo Node,
Wine e o pré-processador necessário às ferramentas gráficas de produção.

As cópias importadas podem conter configurações privadas antigas: são protegidas
por diretórios 0700, não entram na sincronização de projetos com OneDrive e foram
incluídas no conjunto de backup criptografado do servidor. Não confundir inclusão
na rotina com comprovação de um novo snapshot já concluído. Caches de containers
e contextos descartáveis de jobs são excluídos dessa rotina.

## Autenticação do Studio

O login preservado é reutilizado. Se o Studio pedir autenticação, use sua opção
Login via Browser. O helper `/lab/bin/open-url` aceita apenas HTTPS do Roblox e
registra o pedido privado em `/run/user/1001/sasocq-lab/<nome>/browser-request.json`
(no sandbox: `/control/browser-request.json`). Abra esse endereço no Playwright
supervisionado, aproveite a sessão oficial e encaminhe o retorno
`roblox-studio-auth:` ao executável do Studio usando `sasocq-lab launch` na mesma
área. Não exponha URLs com códigos, não copie cookies para o Wine e não publique
um endpoint de callback. Se houver MFA ou reautenticação exigida, o operador deve
concluí-la normalmente. WebView2 não foi instalado como contorno.

## Recuperação dos arquivos transferidos

A raiz importada preserva os caminhos e permissões dos arquivos; a propriedade
no host é codex-worker, deliberadamente. Recupere o conteúdo necessário para um
novo diretório no host com rsync, sem `--delete`, e compare por checksum. Para
uma reversão administrativa excepcional à VM, use o mesmo caminho relativo,
restaure proprietários apropriados (root/configurações, codex-project/fontes,
brotalume-test/perfis antigos) e verifique o manifesto; não sobrescreva versões
mais recentes. Isso não autoriza voltar a desenvolver na VM.

## VM exclusiva para publicação e produção — regra permanente

Por instrução explícita do usuário, a VM `sasocq-server` não é ambiente de desenvolvimento, nem mesmo para tarefas leves.

- Desenvolver, editar código-fonte, instalar dependências de desenvolvimento, compilar, construir imagens, executar testes de desenvolvimento e manter servidores de desenvolvimento/watchers no host, no workspace de Projetos e nos ambientes isolados apropriados. A proteção do sistema principal continua obrigatória; a rede necessária pode ser disponibilizada pelo perfil controlado.
- A VM recebe versões prontas, previamente construídas e verificadas no host. Use-a somente para publicação, execução dos sites/serviços e seus bancos/processamentos, configuração operacional, migrações de dados necessárias ao deploy, monitoramento, manutenção e backups da produção em `sasocq.com`.
- Correções de código também são preparadas no host e entregues como nova versão reversível. Não editar código diretamente na VM como fluxo de desenvolvimento, não executar builds ou suítes de desenvolvimento nela e não instalar IDEs, emuladores ou runtimes de teste como alternativa ao host.
- A validação funcional real após a publicação continua obrigatória no serviço ativo, pelo endereço e fluxo usados pelo usuário. Essa verificação de produção não autoriza transformar a VM em ambiente de desenvolvimento ou staging.
- `ssh sasocq-server` é um meio de implantação e administração da produção. Se faltar capacidade no host para preparar uma entrega, o Sistema deve preparar o ambiente auditável necessário; não transferir o desenvolvimento para a VM.
- Preserve dados, versões operacionais e backups existentes. A regra não autoriza apagar arquivos legados ou interromper serviços publicados. Vale igualmente para o Codex do Sistema, o Codex de Projetos e todos os projetos, substituindo instruções anteriores que permitam desenvolver na VM.
