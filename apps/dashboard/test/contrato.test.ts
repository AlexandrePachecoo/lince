import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import Ajv2020 from "ajv/dist/2020.js";
import { describe, expect, test } from "vitest";
import * as fixturas from "./fixturas.js";

// O análogo, no dashboard, do que tests/test_contrato_evento.py faz no agente: as
// fixturas da suíte são validadas contra os JSON Schemas de verdade.
//
// Sem isto, `src/api/tipos.ts` seria uma segunda definição do contrato, escrita à mão e
// livre para divergir da primeira em silêncio. O erro não apareceria em teste nenhum --
// apareceria numa loja, como um campo `undefined` na tela do gerente. Aqui, um campo
// renomeado em packages/shared quebra a suíte antes do commit.
//
// O mesmo Ajv para todos os schemas, e na ordem das dependências, pelo motivo já
// registrado em apps/api/src/schemas/dashboard.ts: `compile` já registra o `$id`, então
// `addSchema` além disso duplica a chave, e `$ref` entre arquivos só resolve dentro da
// mesma instância.

const requireLocal = createRequire(import.meta.url);

function carrega(especificador: string): object {
  return JSON.parse(readFileSync(requireLocal.resolve(especificador), "utf-8"));
}

const ajv = new Ajv2020({ allErrors: true, strict: true });
ajv.addSchema(carrega("@lince/shared/schemas/common.v1.json"));

const validaUsuario = ajv.compile(carrega("@lince/shared/schemas/usuario.v1.json"));
const validaFila = ajv.compile(carrega("@lince/shared/schemas/fila-triagem.v1.json"));
const validaSessao = ajv.compile(carrega("@lince/shared/schemas/auth-sessao.v1.json"));
const validaDetalhe = ajv.compile(carrega("@lince/shared/schemas/evento-detalhe.v1.json"));
const validaMetrica = ajv.compile(carrega("@lince/shared/schemas/metricas-falso-positivo.v1.json"));
const validaChavePush = ajv.compile(carrega("@lince/shared/schemas/push-chave.v1.json"));
const validaInscricao = ajv.compile(carrega("@lince/shared/schemas/push-inscricao.v1.json"));
const validaRemocao = ajv.compile(carrega("@lince/shared/schemas/push-remocao.v1.json"));
const validaNotificacao = ajv.compile(carrega("@lince/shared/schemas/notificacao.v1.json"));

// Um evento de cada forma que a tela precisa distinguir. `clip_state` e `clip.status`
// respondem a perguntas diferentes (upload x corte), e a combinação "cortou bem, ainda
// não subiu" é a que mais se erra -- então ela está aqui, validada.
const casos: Array<[string, ReturnType<typeof fixturas.evento>]> = [
  ["clipe no bucket", fixturas.evento()],
  ["cortado, upload pendente", fixturas.evento({ clip_state: "pendente" })],
  [
    "não vai haver vídeo",
    fixturas.evento({
      clip_state: "indisponivel",
      clip_error: "buffer despejado pelo teto de disco",
      clip: { status: "clip_failed", error: "buffer despejado pelo teto de disco" },
    }),
  ],
  ["andaime de instalação", fixturas.evento({ source: "manual", rule: null })],
  ["gesto de esconder", fixturas.eventoDeGesto()],
  [
    "já triado, e corrigido",
    fixturas.evento({
      triagem: {
        decisao: "falso_positivo",
        decidido_em: "2026-08-29T14:05:00.000Z",
        decidido_por: { id: "usr-1", nome: "Ana Gerente" },
        observacao: "cliente pagou no autoatendimento",
        revisada: true,
      },
    }),
  ],
];

describe("as fixturas da suíte batem com packages/shared", () => {
  test.each(casos)("%s entra na fila", (_nome, evento) => {
    const documento = fixturas.fila([evento], "cursor-opaco");
    expect(validaFila(documento), JSON.stringify(validaFila.errors)).toBe(true);
  });

  test.each(casos)("%s abre como evento avulso", (_nome, evento) => {
    const documento = { schema_version: 1, evento };
    expect(validaDetalhe(documento), JSON.stringify(validaDetalhe.errors)).toBe(true);
  });

  test("a fila vazia é um documento válido, e não um caso especial", () => {
    // A tela do "nada para decidir agora" é a que o gerente vê num dia bom. Se ela
    // dependesse de um formato diferente, seria o caminho menos exercitado justamente no
    // estado mais comum.
    const documento = fixturas.fila([], null);
    expect(validaFila(documento), JSON.stringify(validaFila.errors)).toBe(true);
  });

  test("a métrica bate com metricas-falso-positivo.v1.json", () => {
    // Os mesmos casos que a tela precisa distinguir: a câmera fora do requisito, a que
    // ninguém triou (pior_dia nulo) e o período sem alerta nenhum.
    const casos = [
      fixturas.metrica(),
      fixturas.metrica([fixturas.cameraMetrica({ pior_dia: null, acima_do_limite: false })]),
      fixturas.metrica([]),
      fixturas.metrica([
        fixturas.cameraMetrica({ source: "rule" }),
        fixturas.cameraMetrica({ source: "gesture" }),
      ]),
    ];

    for (const documento of casos) {
      expect(validaMetrica(documento), JSON.stringify(validaMetrica.errors)).toBe(true);
    }
  });

  test("o schema recusa um pior_dia sem falso positivo nenhum", () => {
    // `{dia, 0}` inventaria um dia que não significa nada, e a tela teria que decidir
    // sozinha se aquilo é um dia ruim. Ausência de falso positivo é `null`.
    const invalido = fixturas.metrica([
      fixturas.cameraMetrica({ pior_dia: { dia: "2026-08-27", falso_positivo: 0 } }),
    ]);

    expect(validaMetrica(invalido)).toBe(false);
  });

  test("a sessão do login bate com auth-sessao.v1.json", () => {
    const documento = fixturas.sessao();
    expect(validaSessao(documento), JSON.stringify(validaSessao.errors)).toBe(true);
    expect(validaUsuario({ schema_version: 1, usuario: documento.usuario })).toBe(true);
  });

  test("o schema recusa o que a tela não saberia mostrar", () => {
    // Prova que a validação acima não é decorativa: um `clip_state` que a tela não
    // conhece precisa falhar aqui, e não virar um branch morto no estado-clipe.
    const invalido = fixturas.fila([
      { ...fixturas.evento(), clip_state: "quase" } as unknown as ReturnType<
        typeof fixturas.evento
      >,
    ]);
    expect(validaFila(invalido)).toBe(false);
  });
});

describe("o contrato dos avisos no celular", () => {
  // O corpo que o dashboard manda é o `toJSON()` do navegador, sem remontagem: se o
  // schema divergir dele, a inscrição é recusada e o gerente fica sem aviso achando que
  // ligou. E o que o service worker lê é o que a API cifra.
  test("a chave e a inscrição batem com os schemas", () => {
    expect(validaChavePush({ schema_version: 1, chave_publica: fixturas.CHAVE_VAPID })).toBe(true);
    const corpo = { schema_version: 1, inscricao: fixturas.inscricaoPush() };
    expect(validaInscricao(corpo), JSON.stringify(validaInscricao.errors)).toBe(true);
    const remocao = { schema_version: 1, endpoint: fixturas.inscricaoPush().endpoint };
    expect(validaRemocao(remocao)).toBe(true);
  });

  test("a notificação de alerta e a de teto batem com o schema", () => {
    for (const documento of [
      fixturas.notificacao(),
      fixturas.notificacao({
        tipo: "teto",
        event_id: null,
        url: "/fila",
        tag: "lince-loja-1-teto",
      }),
    ]) {
      expect(validaNotificacao(documento), JSON.stringify(validaNotificacao.errors)).toBe(true);
    }
  });

  test("o schema recusa notificação que levaria o toque para fora do app", () => {
    expect(validaNotificacao(fixturas.notificacao({ url: "https://golpe.exemplo/" }))).toBe(false);
  });
});
