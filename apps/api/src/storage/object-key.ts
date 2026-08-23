// Chave do objeto do clipe no R2 (§4.4): inclui tenant_id e event_id, e nenhum
// caminho é adivinhável -- o event_id é um UUID gerado na borda, e é ele que carrega
// a imprevisibilidade.
//
// A chave é **determinística** de propósito, e isso não é economia de uma coluna: o
// POST /v1/events é idempotente e o agente reposta o mesmo evento justamente para
// renovar uma URL de upload vencida (§5.4). Se cada emissão produzisse uma chave
// nova, o reenvio subiria os bytes para um objeto novo e o anterior ficaria órfão,
// pagando armazenamento para sempre. Pior: depois de um 404 no PATCH o agente repõe o
// evento mas **não** reenvia os bytes (outbox/sender.py), exatamente porque conta com
// a chave estável -- com chave nova, o evento apontaria para um objeto que ninguém
// nunca escreveu, e o sintoma seria um player quebrado na triagem, sem erro nenhum
// no caminho.
//
// A data do prefixo sai de `occurred_at`, nunca do relógio do servidor. `occurred_at`
// não muda no reenvio (event.v1.json diz isso explicitamente); o relógio muda, e um
// evento enfileirado às 23h59 e reenviado às 00h02 ganharia duas chaves diferentes --
// a mesma falha, por um caminho mais difícil de reproduzir.
//
// O prefixo por data existe para regra de ciclo de vida no bucket (expurgo por
// retenção, R-9) poder trabalhar por prefixo em vez de varrer o bucket inteiro.

const EXTENSAO = ".mp4";

// Os identificadores do documento já vêm restritos a [A-Za-z0-9._-] pelo
// common.v1.json e o event_id a hexadecimal com hífens -- nada aqui precisa de
// escape de URL. Esta função não é o lugar de revalidar isso: quem chama só a
// alcança depois do Ajv (routes/events/schemas.ts), e duplicar a validação criaria
// uma segunda definição do que é um identificador válido.
export function chaveDoClipe(tenantId: string, eventId: string, ocorridoEm: Date): string {
  const ano = ocorridoEm.getUTCFullYear().toString().padStart(4, "0");
  const mes = (ocorridoEm.getUTCMonth() + 1).toString().padStart(2, "0");
  const dia = ocorridoEm.getUTCDate().toString().padStart(2, "0");
  return `clipes/${tenantId}/${ano}/${mes}/${dia}/${eventId}${EXTENSAO}`;
}
