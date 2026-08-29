// O contrato da §4.5 do lado de quem lê. Espelha packages/shared/schemas: cada tipo aqui
// tem um arquivo lá, e é o arquivo que manda.
//
// Escrever o tipo à mão é uma escolha, não preguiça: gerar TypeScript a partir de JSON
// Schema traria um passo de build para manter, e o que se quer evitar não é o trabalho de
// escrever duas vezes -- é a API e o cliente discordarem sem ninguém perceber. Contra isso
// o que vale é o teste: test/contrato.test.ts valida as fixturas da suíte contra o schema
// de verdade, então um campo que muda de nome no contrato quebra aqui, e não na loja.

export type Decisao = "confirmado" | "falso_positivo" | "inconclusivo";

/**
 * Estado do clipe **no armazenamento** — não é o `clip.status`, que é o desfecho do corte
 * na borda. `pendente` é o que o contrato do agente não nomeia: a nuvem conhece o evento,
 * emitiu a URL e os bytes ainda não chegaram.
 */
export type ClipState = "pendente" | "disponivel" | "indisponivel";

export interface Triagem {
  decisao: Decisao;
  decidido_em: string;
  decidido_por: { id: string; nome: string };
  observacao: string | null;
  /** true quando esta não é a primeira decisão: alguém corrigiu, e a tela mostra isso. */
  revisada: boolean;
}

export interface Evento {
  event_id: string;
  tenant_id: string;
  store_id: string;
  camera_id: string;
  occurred_at: string;
  reported_at: string;
  received_at: string;
  /** `manual` é o andaime de gatilho da instalação, e vai marcado na tela (R-1). */
  source: "rule" | "manual";
  rule: { id: string; version: number } | null;
  versions: Record<string, unknown> | null;
  clip_state: ClipState;
  clip_error: string | null;
  clip: { status: string; [campo: string]: unknown } | null;
  /** A decisão **vigente**, ou nula se ninguém triou. */
  triagem: Triagem | null;
}

export interface Fila {
  schema_version: 1;
  eventos: Evento[];
  /** Opaco. Nulo quando acabou. Volta ao GET seguinte em `cursor`. */
  proximo_cursor: string | null;
}

export interface EventoDetalhe {
  schema_version: 1;
  evento: Evento;
}

export interface ClipeUrl {
  schema_version: 1;
  event_id: string;
  url: string;
  expires_in_s: number;
  object_key: string | null;
}

export interface CameraMetrica {
  store_id: string;
  camera_id: string;
  eventos: number;
  confirmado: number;
  falso_positivo: number;
  /** Não soma ao falso positivo: é outro problema, com outra ação (R-3). */
  inconclusivo: number;
  /** Sem isto o zero mente: câmera não triada parece a melhor da loja. */
  pendentes: number;
  falso_positivo_por_dia: number;
  pior_dia: { dia: string; falso_positivo: number } | null;
  /** Se algum **dia** passou do teto. A NFR-2 é teto diário, não média. */
  acima_do_limite: boolean;
}

export interface MetricaFalsoPositivo {
  schema_version: 1;
  periodo: { desde: string; ate: string; dias: number; fuso: string };
  limite_diario: number;
  /** Da pior para a melhor: o topo é onde mexer. */
  cameras: CameraMetrica[];
}

export type Papel = "admin" | "gerente" | "operador";

export interface Usuario {
  id: string;
  tenant_id: string;
  email: string;
  nome: string;
  ativo: boolean;
  /** É esta lista, e não o tenant, que decide o que ele enxerga na fila. */
  lojas: Array<{ store_id: string; papel: Papel }>;
}

export interface Sessao {
  schema_version: 1;
  token: string;
  /** Não há refresh nesta versão: vencido, é login de novo. */
  expires_in_s: number;
  usuario: Usuario;
}
