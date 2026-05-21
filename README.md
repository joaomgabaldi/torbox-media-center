![Logo](https://raw.githubusercontent.com/TorBox-App/torbox-media-center/main/assets/header.png)

Fork do projeto original que faz stream de todos os arquivos, não apenas de arquivos de mídias. Como se fosse um WebDAV porém você consegue fazer stream sem que nada seja baixado para o seu armazenamento. Você pode abrir um executável e ele vai rodar como se fosse um arquivo local.

Dentre os ajustes que eu fiz, eu limitei a "agressividade" com que a API é contatada (sobretudo com arquivos onde o Windows faz leitura aleatória), senão a API retorna erro 429 Too Many Requests.
