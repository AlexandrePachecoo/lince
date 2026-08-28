import { AppError } from "../../app-error.js";

// 404 no PATCH /v1/events/{event_id} tem significado combinado com o agente, e não é
// "não achei": classify_response traduz 404 em UNKNOWN_EVENT, e o agente responde
// **repondo o evento inteiro** na fila -- sem reenviar os bytes do clipe, porque a
// chave do objeto é determinística e o que já subiu continua no lugar (§4.4).
//
// Por isso um evento de OUTRO tenant também responde 404, nunca 403: além de não
// vazar a existência da linha, é o único status que leva o agente a fazer a coisa
// certa. O caminho de reposição termina no POST, que aí sim recusa o escopo errado
// com 403 (auth/errors.ts, EscopoDoAgente) -- a decisão de escopo acontece num lugar
// só.
export class EventoDesconhecido extends AppError {
  readonly status = 404;
  constructor() {
    super("evento desconhecido");
  }
}

// 409, e não 404: o evento existe e o triador tem acesso a ele -- o que não existe (ou
// ainda não) é o vídeo. A distinção importa para o dashboard, que trata os dois casos de
// forma diferente: em `pendente` mostra "processando" e tenta de novo, em `indisponivel`
// libera a decisão sem vídeo em vez de deixar o gerente esperando (§4.5).
// A mensagem vem de quem lança (mesmo padrão de RequisicaoInvalida), porque as duas
// razões pedem ações diferentes de quem lê.
export class ClipeNaoDisponivel extends AppError {
  readonly status = 409;
}
