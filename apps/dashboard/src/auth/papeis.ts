import type { Usuario } from "../api/tipos.js";

/**
 * Lojas em que o usuário é `admin`. `admin` é papel de loja, não de tenant: quem
 * administra a loja A não instala box nem cadastra gente na loja B, e a API recusa com
 * `403` quem tentar. Esta lista decide só o que a tela **oferece** — o que autoriza é a
 * API, que relê os vínculos do banco a cada requisição.
 */
export function lojasAdministradas(usuario: Usuario): string[] {
  return usuario.lojas.filter((vinculo) => vinculo.papel === "admin").map((v) => v.store_id);
}
