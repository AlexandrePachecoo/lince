import assert from "node:assert/strict";
import { test } from "node:test";
import { agrega } from "../../src/metricas/agrega.js";
import { janelaDeDias } from "../../src/metricas/dias.js";
import type { LinhaPorDia } from "../../src/metricas/falso-positivo-query.js";

// As duas decisões que fazem esta métrica dizer a verdade ou mentir: o denominador da
// média e o que conta como "acima do limite". Ambas são fáceis de trocar por algo que
// parece equivalente e não é.

const SP = "America/Sao_Paulo";
const SEMANA = janelaDeDias(7, SP, new Date("2026-08-29T15:00:00.000Z"));

function linha(sobrescreve: Partial<LinhaPorDia> = {}): LinhaPorDia {
  return {
    lojaId: "loja-centro",
    cameraId: "cam3",
    dia: "2026-08-29",
    confirmado: 0,
    falsoPositivo: 0,
    inconclusivo: 0,
    pendentes: 0,
    ...sobrescreve,
  };
}

test("a NFR-2 é julgada pelo pior dia, não pela média", () => {
  // O caso que decide o desenho inteiro. Nove alertas falsos numa noite e zero no resto
  // da semana dá média 1,3 -- dentro do limite de 3, aparentemente saudável. Mas quem
  // recebeu os nove naquela noite já desligou a notificação, e é assim que o R-1 mata o
  // produto. O requisito é um teto diário, e o teste guarda essa leitura.
  const documento = agrega([linha({ dia: "2026-08-27", falsoPositivo: 9 })], SEMANA, SP);
  const camera = documento.cameras[0];

  assert.ok(camera);
  assert.equal(camera.acima_do_limite, true);
  assert.deepEqual(camera.pior_dia, { dia: "2026-08-27", falso_positivo: 9 });
  assert.ok(camera.falso_positivo_por_dia < 3, "e a média sozinha diria que está tudo bem");
});

test("o denominador são os dias corridos, não os dias com evento", () => {
  // Dividir por dias-com-evento faria uma câmera com uma única noite ruim parecer que
  // alerta assim todo dia, e mandaria recalibrar a câmera errada.
  const documento = agrega([linha({ falsoPositivo: 7 })], SEMANA, SP);

  assert.equal(documento.cameras[0]?.falso_positivo_por_dia, 1, "7 em 7 dias, não 7 em 1");
});

test("câmera constante no limite não é marcada, e um dia acima é", () => {
  // A fronteira exata do requisito: "<= 3" passa, 4 não. Um erro de sinal aqui marcaria a
  // loja inteira como problemática, e a tela deixaria de significar alguma coisa.
  const noLimite = agrega(
    [
      linha({ dia: "2026-08-27", falsoPositivo: 3 }),
      linha({ dia: "2026-08-28", falsoPositivo: 3 }),
    ],
    SEMANA,
    SP,
  );
  const acima = agrega([linha({ dia: "2026-08-27", falsoPositivo: 4 })], SEMANA, SP);

  assert.equal(noLimite.cameras[0]?.acima_do_limite, false);
  assert.equal(acima.cameras[0]?.acima_do_limite, true);
});

test("inconclusivo não conta como falso positivo", () => {
  // "Não dá para ver nada no clipe" não é o mesmo que "o alerta estava errado". Somá-los
  // inflaria a métrica com um problema que tem outra causa e outra ação: inconclusivo
  // alto é ângulo, luz ou posicionamento (R-3), não limiar.
  const documento = agrega(
    [linha({ falsoPositivo: 2, inconclusivo: 5, confirmado: 1 })],
    SEMANA,
    SP,
  );
  const camera = documento.cameras[0];

  assert.equal(camera?.falso_positivo, 2);
  assert.equal(camera?.inconclusivo, 5);
  assert.equal(camera?.acima_do_limite, false, "5 inconclusivos não estouram o teto de 3");
});

test("pendentes aparecem, senão o zero mente", () => {
  // Uma câmera que ninguém triou mostra zero falso positivo e parece a melhor da loja.
  // Sem os pendentes ao lado, a tela premiaria justamente a fila abandonada.
  const documento = agrega([linha({ pendentes: 40 })], SEMANA, SP);
  const camera = documento.cameras[0];

  assert.equal(camera?.falso_positivo, 0);
  assert.equal(camera?.pendentes, 40);
  assert.equal(camera?.eventos, 40, "e os 40 contam como eventos que aconteceram");
});

test("a lista vem da pior para a melhor", () => {
  // Quem abre esta tela quer saber onde mexer na segunda de manhã. O topo tem que ser a
  // resposta, não a primeira câmera em ordem alfabética.
  const documento = agrega(
    [
      linha({ cameraId: "cam1", dia: "2026-08-27", falsoPositivo: 1 }),
      linha({ cameraId: "cam2", dia: "2026-08-27", falsoPositivo: 9 }),
      linha({ cameraId: "cam3", dia: "2026-08-27", falsoPositivo: 4 }),
    ],
    SEMANA,
    SP,
  );

  assert.deepEqual(
    documento.cameras.map((c) => c.camera_id),
    ["cam2", "cam3", "cam1"],
  );
});

test("a ordem é estável entre duas leituras da mesma janela", () => {
  // Uma lista que se reordena sozinha entre dois refreshes não dá para conferir com
  // ninguém: o gerente aponta "a segunda linha" e ela já é outra câmera.
  const linhas = [
    linha({ cameraId: "cam-b", falsoPositivo: 2 }),
    linha({ cameraId: "cam-a", falsoPositivo: 2 }),
    linha({ cameraId: "cam-c", falsoPositivo: 2 }),
  ];

  const primeira = agrega(linhas, SEMANA, SP).cameras.map((c) => c.camera_id);
  const segunda = agrega([...linhas].reverse(), SEMANA, SP).cameras.map((c) => c.camera_id);

  assert.deepEqual(primeira, segunda);
  assert.deepEqual(primeira, ["cam-a", "cam-b", "cam-c"], "empate resolve pelo id");
});

test("os dias de uma câmera somam numa linha só", () => {
  const documento = agrega(
    [
      linha({ dia: "2026-08-27", falsoPositivo: 2, confirmado: 1 }),
      linha({ dia: "2026-08-28", falsoPositivo: 3, pendentes: 4 }),
    ],
    SEMANA,
    SP,
  );

  assert.equal(documento.cameras.length, 1);
  assert.equal(documento.cameras[0]?.falso_positivo, 5);
  assert.equal(documento.cameras[0]?.eventos, 10);
  assert.deepEqual(documento.cameras[0]?.pior_dia, { dia: "2026-08-28", falso_positivo: 3 });
});

test("a mesma câmera em duas lojas não se mistura", () => {
  // `camera_id` é único dentro da loja, não na rede: duas lojas têm "cam1" (§5.2). Somar
  // as duas atribuiria a uma loja o falso positivo da outra.
  const documento = agrega(
    [
      linha({ lojaId: "loja-a", cameraId: "cam1", falsoPositivo: 1 }),
      linha({ lojaId: "loja-b", cameraId: "cam1", falsoPositivo: 8 }),
    ],
    SEMANA,
    SP,
  );

  assert.equal(documento.cameras.length, 2);
  assert.equal(documento.cameras[0]?.store_id, "loja-b");
});

test("sem falso positivo nenhum, o pior dia é nulo e não o dia zero", () => {
  // `null` diz "não houve"; um `{dia, 0}` inventaria um dia que não significa nada e a
  // tela teria que decidir sozinha se aquilo é um dia ruim.
  const documento = agrega([linha({ confirmado: 3 })], SEMANA, SP);

  assert.equal(documento.cameras[0]?.pior_dia, null);
  assert.equal(documento.cameras[0]?.acima_do_limite, false);
});

test("período sem evento nenhum devolve lista vazia, com o período preenchido", () => {
  // A tela do dia bom. Ela precisa saber qual janela foi consultada para dizer "nenhum
  // alerta nos últimos 7 dias" em vez de um vazio que se confunde com falha.
  const documento = agrega([], SEMANA, SP);

  assert.deepEqual(documento.cameras, []);
  assert.equal(documento.periodo.dias, 7);
  assert.equal(documento.periodo.fuso, SP);
  assert.equal(documento.limite_diario, 3);
});
