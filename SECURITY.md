# Segurança

Não publique vulnerabilidades em issues. Envie o relato para
`security@sasocq.com`, com versão, impacto e passos mínimos de reprodução.

O perfil **Sistema + Projetos** concede ao usuário `codex` acesso integral ao
`sudo` sem senha. Essa é uma escolha explícita da instalação e não deve ser
ativada em máquinas compartilhadas sem compreender esse risco.

Os pacotes publicados nunca devem conter tokens, cookies, chaves privadas,
conversas, perfis de navegador ou configurações pessoais. A suíte de testes
inclui uma varredura automática desses padrões antes de qualquer release.

