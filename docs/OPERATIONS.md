# Operação do WorkHunter

## Executar

```bash
python monitor.py --once
```

O processo normal usa `check_interval_minutes` do `config.json`. O limite de uma rodada é `max_cycle_seconds`; ultrapassá-lo gera alerta no relatório, mas não interrompe uma coleta em andamento.

## Configuração

- `config.json`: fontes, consultas, filtros, janelas recentes e limites.
- `profile.json`: formação, localização, stack e empresas prioritárias.
- `.env`: credenciais locais, nunca commitadas.
- `seen_jobs.json`: estado operacional local, ignorado pelo Git.
- `cycle_health.json`: resumo da última rodada, com duração, saúde das fontes,
  funil de descarte e entregas; é regenerado ao final de cada ciclo bem-sucedido.

O coletor público do LinkedIn continua limitado a uma busca por janela de seis
horas. `linkedin_detail_enrichment_enabled` habilita a tentativa de obter a
descrição apenas para cartões de vagas de software com sinais de aderência e
evidência incompleta; `linkedin_detail_enrichment_limit` define o teto total de
detalhes por ciclo (configurado em 2). Vagas sênior e títulos fora do escopo não
consomem esse orçamento. Respostas HTTP 429 interrompem novas tentativas e
acionam o cooldown persistente da fonte.

### Fallback de entrega

O Telegram é o canal primário. Quando o envio retorna falha, o monitor tenta
enviar o mesmo alerta pela API do Resend usando `RESEND_API_KEY`,
`ALERT_EMAIL_FROM` e `ALERT_EMAIL_TO`. Se os dois canais falharem, a vaga não é
marcada como entregue e permanece disponível para retry com backoff. Sem as três
variáveis de e-mail configuradas, o fallback fica desativado de forma segura.

### Consultas recentes

Cada monitor pode declarar `query_variants`, `seniority_variants` e `recent_window_hours`.
O planejador gera uma consulta para cada combinação e o ciclo registra o termo,
a senioridade, a janela aplicada e o resultado por consulta. No LinkedIn, a janela
é convertida para `f_TPR` em segundos; um `time_range` explícito tem prioridade.
No Gupy, os parâmetros seguem para a busca sem o `query_id` interno.

## Rollback

Cada alteração de código é publicada em commit separado. Para voltar a uma versão anterior, primeiro identifique o commit e use uma reversão explícita:

```bash
git log --oneline -10
git revert <commit>
python -m pytest -q
```

Mudanças de configuração podem ser revertidas restaurando o commit anterior de `config.json`, sempre rodando a suíte depois.

## Verificação

```bash
python -m pytest -q
git status --short
```

O arquivo `seen_jobs.json` pode mudar após uma execução real e deve permanecer local. Falhas de entrega ficam registradas para retry com backoff e limite de tentativas.

## Worker do Telegram

O worker `workhunter-inbox.service` usa long polling da API do Telegram por
até 45 segundos. Quando não há mensagens, ele permanece aguardando sem iniciar
coletas nem consumir ciclos completos. Comandos respondem diretamente; URLs
recebidas entram na fila e executam somente a análise manual.

## Métricas

O relatório de cada ciclo mostra descoberta bruta, vagas únicas, duplicatas implícitas, descartes por motivo, alertas entregues, chamadas LLM, duração e saúde por fonte. Precisão e recall reais exigem feedback humano sobre relevância e candidaturas; o sistema não os inventa a partir de contagens de filtro.

Use `python workhunter_cli.py audit-sources --days 7` para auditar o desempenho
retido por fonte. O relatório separa tentativas de enriquecimento (úteis, sem
dados, HTTP 429 e timeout) das buscas e agrupa RSS pelo hostname, incluindo
timeouts e falhas consecutivas. Para Gupy, o resumo do ciclo informa datas
recuperadas e datas que permaneceram desconhecidas; com exigência de data
verificável, essas vagas são rejeitadas por recência e não entram como recentes.
HTTP 429 ao abrir detalhes do LinkedIn pausa somente o enriquecimento por seis
horas; as buscas permanecem ativas. HTTP 429 nas próprias buscas continua usando
o cooldown integral da fonte.
