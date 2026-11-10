# Ofertas da Madrugada · Outlet360

Página **madrugada.outlet360.com.br**: 20 peças selecionadas toda noite. A seleção entra às 20h e fica no ar até as 20h do dia seguinte (horário de Brasília).

- `madrugada/gerar.py` escolhe os produtos (mais vendidos no GA4 + maiores descontos, só com P/M/G em estoque no Tiny) e gera a página.
- `madrugada/pagina.html` é o modelo da página.
- `docs/` é o que vai ao ar (GitHub Pages publicado pelo próprio robô). Gerado automaticamente: não editar à mão.
- `.github/workflows/madrugada.yml` roda sozinho: seleção às 19h, checagem de preço/estoque de hora em hora à noite e 4x durante o dia.

Chaves de API ficam em **Settings → Secrets and variables → Actions** (`TINY_API_TOKEN`, `WINDSOR_API_KEY`).
Para republicar na hora: edite `madrugada/publicar-agora.txt`.
