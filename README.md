# Steam Sale Ranker

Ranker de promoções da Steam com score composto baseado em qualidade, popularidade e desconto.

## Como funciona

```
score = (review% / 100) × log10(total_reviews + 1) × (1 + desconto / 200)
```

- **review%** — qualidade percebida (peso maior)
- **log10(reviews)** — popularidade (cresce devagar: 100k reviews ≠ 10× melhor que 10k)
- **desconto** — bônus de 0.5% por ponto de desconto (50% off = +25% no score)

Os jogos são agrupados pelos blocos oficiais de avaliação da Steam:

| Bloco | Critério |
|---|---|
| Overwhelmingly Positive | ≥ 95% com 500+ reviews |
| Very Positive | 80–94% com 500+ reviews |
| Mostly Positive | 70–79% |
| Mixed | 40–69% |
| Mostly Negative | 20–39% |
| Overwhelmingly Negative | < 20% com 500+ reviews |

Jogos marcados com **BAIXA HISTÓRICA** estão no preço mais baixo já registrado (via CheapShark API).

## Requisitos

```bash
pip install requests beautifulsoup4
```

Python 3.10+

## Uso

```bash
# Terminal — 10 páginas (~500 jogos)
python steam_sale_ranker.py

# Terminal — N páginas
python steam_sale_ranker.py 20

# Gera relatório HTML
python steam_sale_ranker.py 20 --html
```

**Windows:** execute `steam_sale.bat` direto (sem precisar do terminal).

## Output

- **Terminal:** tabela colorida por bloco, top 30 por categoria
- **HTML (`--html`):** relatório completo com imagens, preços originais, preço atual, baixa histórica em BRL e link direto para a loja

## Filtros aplicados

- Mínimo **2.000 reviews** (ignora jogos obscuros)
- Mínimo **15% de desconto**
- Duas passagens de coleta: ordenado por reviews e por relevância Steam (para cobrir mais jogos)

## Dependências externas

- **Steam Store Search API** — coleta de jogos em promoção
- **CheapShark API** — verificação de baixa histórica de preço (rate limit respeitado: 2 req/s)
