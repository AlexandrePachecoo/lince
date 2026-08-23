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
