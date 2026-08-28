// Normalização do e-mail, aplicada **antes** da validação de schema e não depois.
//
// A ordem é o ponto. O contrato (auth-login.v1.json) descreve o e-mail já normalizado,
// que é o que um cliente bem-comportado manda; a API é tolerante com o que chega do
// mundo real. Validar primeiro recusaria com 400 o gerente que digitou o próprio e-mail
// no celular -- o teclado capitaliza a primeira letra sozinho e o autocompletar costuma
// deixar um espaço no fim -- e o recado na tela falaria de "formato inválido" sobre um
// endereço que está visivelmente certo.
//
// Cadastro e login normalizam pela mesma função, e é isso que importa: se divergissem,
// existiria conta criada com um endereço que nenhum login alcança.

export function normalizaEmail(email: string): string {
  return email.trim().toLowerCase();
}

/** Devolve o corpo com `email` normalizado, ou intocado se ele não for texto — nesse
 * caso quem recusa é a validação de schema, com a mensagem dela. */
export function comEmailNormalizado(corpo: unknown): unknown {
  if (typeof corpo !== "object" || corpo === null || Array.isArray(corpo)) {
    return corpo;
  }
  const registro = corpo as Record<string, unknown>;
  if (typeof registro.email !== "string") {
    return corpo;
  }
  return { ...registro, email: normalizaEmail(registro.email) };
}
