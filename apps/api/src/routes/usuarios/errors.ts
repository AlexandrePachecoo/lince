import { AppError } from "../../app-error.js";

// 404 também para usuário de outro tenant, pelo mesmo motivo de EventoDesconhecido: não
// vazar a existência da linha. Aqui o vazamento seria pior -- confirmaria que um e-mail
// tem conta em outra rede de mercados.
export class UsuarioDesconhecido extends AppError {
  readonly status = 404;
  constructor() {
    super("usuário desconhecido");
  }
}

// Só é alcançável com o cadastro inconsistente (um vínculo apontando para loja de outro
// tenant). Existe para que essa inconsistência apareça como erro nomeado em vez de virar
// uma violação de chave estrangeira lá embaixo, que no log parece bug do Prisma.
export class LojaDesconhecida extends AppError {
  readonly status = 404;
  constructor(lojaId: string) {
    super(`loja desconhecida neste tenant: ${lojaId}`);
  }
}
