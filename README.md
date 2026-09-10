# AI-Field-Translator

Fork modificado do add-on do Anki AI Field Translator, com melhorias de desempenho e suporte a outro provedor como fallback.

## Autoria e projeto

- Autor original: Josscii
- Fork e manutenção: V2power
- Projeto original: https://github.com/josscii/anki-ai-field-translator
- Este fork: https://github.com/V2power/AI-Field-Translator
- Licença: GNU AGPL v3 ou posterior

## Funcionamento

O add-on traduz o conteúdo de um campo de uma nota do Anki e salva o resultado em outro campo. Para isso, você escolhe o tipo de nota, o campo de origem, o campo de destino e o idioma da tradução.

O Gemini é usado como provedor principal. Um provedor alternativo pode ser configurado para ser usado automaticamente quando o principal falhar ou atingir seu limite de uso.

## Instalação

1. Feche o Anki.
2. Copie a pasta do add-on para a pasta `addons21` do seu perfil do Anki.
3. Abra o Anki novamente.

Se estiver instalando a partir de um ZIP, os arquivos `__init__.py`, `manifest.json` e `config.json` devem ficar diretamente dentro da pasta do add-on.

## Configuração e uso

1. No Anki, abra **Ferramentas > AI Field Translator**.
2. Na aba **Settings**, informe a URL da API, a chave e o modelo principal.
3. Configure o provedor alternativo, se desejar, e salve as configurações.
4. Na aba **Translation**, clique em **Add Mapping**.
5. Escolha o tipo de nota, o campo de origem, o campo de destino e o idioma.
6. Mantenha marcada a opção de ignorar campos de destino já preenchidos para evitar sobrescrever conteúdo existente.
7. Clique em **Start Translation** e acompanhe o progresso.

Faça um backup antes do primeiro uso. As chaves de API são credenciais pessoais e não devem ser publicadas no GitHub.
