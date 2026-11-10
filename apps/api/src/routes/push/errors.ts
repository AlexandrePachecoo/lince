import { AppError } from "../../app-error.js";

/** A API subiu sem chaves VAPID. 503, e não 404: a rota existe e o recurso é que está
 * desligado neste servidor — o dashboard mostra "notificação indisponível" em vez de
 * tratar como bug. */
export class PushDesligado extends AppError {
  readonly status = 503;
  constructor() {
    super("notificação por Web Push desligada neste servidor");
  }
}

/** Endpoint fora da lista de serviços de push. Ver `endpointPermitido`. */
export class EndpointNaoPermitido extends AppError {
  readonly status = 400;
  constructor() {
    super("endpoint de inscrição não é de um serviço de push conhecido");
  }
}
