// O dia civil da loja, e a janela UTC que corresponde a ele.
//
// Tudo no sistema guarda instante UTC (`common.v1.json`), e está certo. Mas "3 alertas
// falsos por câmera **por dia**" (NFR-2) é uma afirmação sobre o dia de quem trabalha na
// loja, não sobre o dia do meridiano de Greenwich. Em UTC o corte cai às 21h no horário
// de Brasília, bem no movimento da noite: a segunda-feira de uma câmera seria contada
// metade em cada dia, e uma câmera acima do limite apareceria dentro dele nos dois.
//
// Sem biblioteca de data: o que se precisa aqui é do fuso do IANA aplicado a um instante,
// e o `Intl` do próprio Node tem a base tz completa. Uma dependência a mais para
// converter duas datas seria superfície para manter (R-11).

/** Se o Node conhece este identificador de fuso. */
export function fusoValido(fuso: string): boolean {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: fuso });
    return true;
  } catch {
    // `RangeError: Invalid time zone specified`. É a única forma de perguntar isso ao
    // ICU -- não há lista exposta de forma portátil em todo runtime.
    return false;
  }
}

const FORMATO = new Map<string, Intl.DateTimeFormat>();

function formatador(fuso: string): Intl.DateTimeFormat {
  // Construir um DateTimeFormat custa (carrega dados do ICU), e a agregação chama isto
  // por loja. Memorizar por fuso é barato e o objeto é imutável.
  let existente = FORMATO.get(fuso);
  if (existente === undefined) {
    existente = new Intl.DateTimeFormat("en-US", {
      timeZone: fuso,
      hour12: false,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    FORMATO.set(fuso, existente);
  }
  return existente;
}

interface Partes {
  ano: number;
  mes: number;
  dia: number;
  hora: number;
  minuto: number;
  segundo: number;
}

function partesLocais(instante: Date, fuso: string): Partes {
  const encontradas: Record<string, number> = {};
  for (const parte of formatador(fuso).formatToParts(instante)) {
    if (parte.type !== "literal") {
      encontradas[parte.type] = Number(parte.value);
    }
  }
  return {
    ano: encontradas.year ?? 0,
    // `hour12: false` produz hora 24 para a meia-noite em alguns runtimes do ICU. 24:00
    // do dia D é 00:00 do dia D, e não do seguinte -- só a hora precisa do ajuste, e o
    // dia já vem certo do próprio formatador.
    mes: encontradas.month ?? 1,
    dia: encontradas.day ?? 1,
    hora: (encontradas.hour ?? 0) % 24,
    minuto: encontradas.minute ?? 0,
    segundo: encontradas.second ?? 0,
  };
}

function doisDigitos(valor: number): string {
  return valor.toString().padStart(2, "0");
}

/** O dia civil (`YYYY-MM-DD`) em que este instante cai, no fuso dado. */
export function diaLocal(instante: Date, fuso: string): string {
  const p = partesLocais(instante, fuso);
  return `${p.ano.toString().padStart(4, "0")}-${doisDigitos(p.mes)}-${doisDigitos(p.dia)}`;
}

/**
 * O instante UTC da meia-noite que abre o dia `YYYY-MM-DD` no fuso dado.
 *
 * O caminho é indireto de propósito: não dá para perguntar ao `Intl` "qual o UTC deste
 * horário local" — ele só converte na direção contrária. Então se estima o UTC, mede-se
 * o deslocamento que o fuso aplicou a essa estimativa, e corrige-se. A segunda passada
 * existe para o dia de mudança de horário de verão, em que o deslocamento da estimativa
 * e o do resultado são diferentes; o Brasil não tem mais horário de verão, mas o código
 * não pode depender disso — a decisão foi política e pode voltar.
 */
export function inicioDoDiaUtc(dia: string, fuso: string): Date {
  const [ano, mes, diaDoMes] = dia.split("-").map(Number) as [number, number, number];
  const desejado = Date.UTC(ano, mes - 1, diaDoMes, 0, 0, 0);

  let utc = desejado - deslocamento(new Date(desejado), fuso);
  utc = desejado - deslocamento(new Date(utc), fuso);
  return new Date(utc);
}

/** Quanto o fuso adianta (ou atrasa) em relação ao UTC neste instante, em milissegundos. */
function deslocamento(instante: Date, fuso: string): number {
  const p = partesLocais(instante, fuso);
  const comoSeFosseUtc = Date.UTC(p.ano, p.mes - 1, p.dia, p.hora, p.minuto, p.segundo);
  // O truncamento para segundos é o que o formatador entrega; instante nenhum de fuso do
  // IANA tem deslocamento com fração de segundo desde 1972.
  return comoSeFosseUtc - Math.floor(instante.getTime() / 1000) * 1000;
}

export interface Janela {
  /** Primeiro dia do período, no fuso da loja. */
  diaInicial: string;
  /** Último dia, inclusivo — o de hoje, ainda que incompleto. */
  diaFinal: string;
  /** Quantos dias corridos o período tem. É o denominador da média. */
  dias: number;
  /** Instante UTC que abre o período, inclusivo. */
  desde: Date;
  /** Instante UTC que fecha o período, **exclusivo** — a meia-noite do dia seguinte. */
  ate: Date;
}

const DIA_MS = 86_400_000;

/**
 * Os últimos `dias` dias civis da loja, terminando no dia de hoje (inclusive).
 *
 * `ate` é exclusivo e aponta para a meia-noite do dia seguinte, e não para 23:59:59.999
 * de hoje: um evento gravado no último milissegundo do dia cairia fora de um limite
 * inclusivo escrito com casas decimais, e some da métrica sem deixar rastro.
 */
export function janelaDeDias(dias: number, fuso: string, agora: Date): Janela {
  const diaFinal = diaLocal(agora, fuso);
  const inicioDoFinal = inicioDoDiaUtc(diaFinal, fuso);

  // Meio-dia local como âncora para andar no calendário, nunca a meia-noite. Um dia de
  // mudança de horário de verão tem 23 ou 25 h: somar ou subtrair múltiplos de 24 h a
  // partir da meia-noite cai no dia vizinho, e o período sairia com um dia a mais ou a
  // menos -- silenciosamente, porque a resposta continuaria parecendo uma semana.
  const meioDia = inicioDoFinal.getTime() + DIA_MS / 2;
  const diaInicial = diaLocal(new Date(meioDia - (dias - 1) * DIA_MS), fuso);
  const diaSeguinte = diaLocal(new Date(meioDia + DIA_MS), fuso);

  return {
    diaInicial,
    diaFinal,
    dias,
    desde: inicioDoDiaUtc(diaInicial, fuso),
    // A meia-noite que abre o dia seguinte, calculada pelo calendário e não somando 24 h:
    // é o limite exclusivo, e é o que garante que um evento do último milissegundo do dia
    // continue dentro do período.
    ate: inicioDoDiaUtc(diaSeguinte, fuso),
  };
}
