// Como o instante do evento aparece na tela.
//
// A fila mostra "há 4 min", e não "14:32". Não é enfeite: o que o gerente decide ao ler a
// fila é se ainda dá para agir — olhar a câmera, encontrar alguém no corredor — e essa
// pergunta é sobre distância no tempo, não sobre hora do relógio. A hora exata fica no
// `title` e na tela do evento, onde ela vira registro.

const MINUTO = 60_000;
const HORA = 60 * MINUTO;
const DIA = 24 * HORA;

export function quandoFoi(iso: string, agora: number = Date.now()): string {
  const instante = new Date(iso).getTime();
  if (Number.isNaN(instante)) {
    return iso;
  }

  const decorrido = agora - instante;

  // Negativo significa relógio do box adiantado em relação ao do celular (§5.3 mede essa
  // deriva). "Daqui a 3 min" para um furto que já aconteceu faria o gerente duvidar do
  // app inteiro; "agora" é impreciso do jeito certo.
  if (decorrido < MINUTO) {
    return "agora";
  }
  if (decorrido < HORA) {
    return `há ${Math.floor(decorrido / MINUTO)} min`;
  }
  if (decorrido < DIA) {
    const horas = Math.floor(decorrido / HORA);
    return horas === 1 ? "há 1 hora" : `há ${horas} horas`;
  }

  const dias = Math.floor(decorrido / DIA);
  return dias === 1 ? "ontem" : `há ${dias} dias`;
}

/** Data e hora legíveis, para o `title` da fila e para o registro na tela do evento. */
export function instanteCurto(iso: string): string {
  const data = new Date(iso);
  if (Number.isNaN(data.getTime())) {
    return iso;
  }
  return data.toLocaleString("pt-BR", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
