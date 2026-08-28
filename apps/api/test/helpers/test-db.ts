import { randomBytes, randomUUID } from "node:crypto";
import { type PapelUsuario, PrismaClient } from "@prisma/client";
import { hashToken } from "../../src/auth/agent-token.js";
import { geraHashSenha } from "../../src/auth/senha.js";

// Isolamento de teste: mesmo Postgres de infra/docker-compose.yml, schema separado
// (TEST_DATABASE_URL, lince_test) do que o dev usa na mão via `pnpm dev`/`pnpm seed`.
// TRUNCATE entre testes em vez de transação-por-teste: mais simples de manter sozinho
// e não exige trocar como o PrismaClient é injetado no app (ver server.ts).
//
// Isso só é seguro com os arquivos de teste rodando em série: dois arquivos em
// paralelo truncando a mesma tabela por baixo um do outro derruba o outro com FK
// violation, não com falha de asserção -- por isso `pnpm test` roda com
// --test-concurrency=1 (package.json). Dentro de um arquivo os testes já rodam em
// série por padrão do node:test.

const databaseUrl = process.env.TEST_DATABASE_URL;
if (!databaseUrl) {
  throw new Error(
    "TEST_DATABASE_URL não definida -- copie apps/api/.env.example para .env e rode " +
      "`DATABASE_URL=$TEST_DATABASE_URL pnpm exec prisma migrate deploy` uma vez.",
  );
}

export const prismaTeste = new PrismaClient({ datasources: { db: { url: databaseUrl } } });

export async function limpaBanco(): Promise<void> {
  await prismaTeste.$executeRawUnsafe(
    'TRUNCATE TABLE "auditoria", "triagem", "usuario_loja", "usuario", "evento", ' +
      '"agente", "token_bootstrap", "camera", "loja", "tenant" RESTART IDENTITY CASCADE',
  );
}

export interface LojaSemeada {
  tenantId: string;
  lojaId: string;
  token: string;
}

export interface SemeiaLojaOpcoes {
  tenantId?: string;
  lojaId?: string;
  ativo?: boolean;
  detection?: object;
  tracking?: object;
  cameras?: Array<{
    cameraId: string;
    url?: string;
    rules?: object;
  }>;
}

// Cria tenant/loja/agente (+ opcionalmente câmeras) com IDs únicos por chamada, e
// devolve o token em claro -- só o teste vê isso; o banco guarda apenas o hash
// (mesma rotina de produção, auth/agent-token.ts, nunca uma versão "de teste" dela).
export async function semeiaLoja(opcoes: SemeiaLojaOpcoes = {}): Promise<LojaSemeada> {
  const sufixo = randomUUID().slice(0, 8);
  const tenantId = opcoes.tenantId ?? `tenant-${sufixo}`;
  const lojaId = opcoes.lojaId ?? `loja-${sufixo}`;
  const token = `token-teste-${randomBytes(16).toString("hex")}`;

  await prismaTeste.tenant.upsert({
    where: { id: tenantId },
    create: { id: tenantId, nome: `Tenant ${tenantId}` },
    update: {},
  });

  await prismaTeste.loja.upsert({
    where: { id: lojaId },
    create: {
      id: lojaId,
      tenantId,
      nome: `Loja ${lojaId}`,
      ...(opcoes.detection !== undefined ? { detection: opcoes.detection } : {}),
      ...(opcoes.tracking !== undefined ? { tracking: opcoes.tracking } : {}),
    },
    update: {},
  });

  for (const camera of opcoes.cameras ?? []) {
    await prismaTeste.camera.upsert({
      where: { lojaId_cameraId: { lojaId, cameraId: camera.cameraId } },
      create: {
        lojaId,
        cameraId: camera.cameraId,
        url: camera.url ?? `rtsp://localhost:8554/${camera.cameraId}`,
        ...(camera.rules !== undefined ? { rules: camera.rules } : {}),
      },
      update: {},
    });
  }

  await prismaTeste.agente.create({
    data: {
      lojaId,
      tokenHash: hashToken(token),
      tokenPrefix: token.slice(0, 8),
      ativo: opcoes.ativo ?? true,
    },
  });

  return { tenantId, lojaId, token };
}

export interface TokenBootstrapSemeado {
  lojaId: string;
  token: string;
}

export interface SemeiaTokenBootstrapOpcoes {
  lojaId?: string;
  expiraEm?: Date;
  usadoEm?: Date | null;
}

// expiraEm/usadoEm no passado são semeados direto -- sem sleep, sem relógio falso: o
// que o teste de expiração/reuso precisa é do estado da linha, não de tempo real
// decorrido (CLAUDE.md, "nunca use sleep para sincronizar teste").
export async function semeiaTokenBootstrap(
  opcoes: SemeiaTokenBootstrapOpcoes = {},
): Promise<TokenBootstrapSemeado> {
  const lojaId = opcoes.lojaId ?? (await semeiaLoja()).lojaId;
  const token = `bootstrap-teste-${randomBytes(16).toString("hex")}`;

  await prismaTeste.tokenBootstrap.create({
    data: {
      lojaId,
      tokenHash: hashToken(token),
      tokenPrefix: token.slice(0, 8),
      expiraEm: opcoes.expiraEm ?? new Date(Date.now() + 60 * 60 * 1000),
      usadoEm: opcoes.usadoEm ?? null,
    },
  });

  return { lojaId, token };
}

export interface UsuarioSemeado {
  usuarioId: string;
  tenantId: string;
  email: string;
  senha: string;
  nome: string;
}

export interface SemeiaUsuarioOpcoes {
  tenantId: string;
  /** lojaId -> papel. Vazio é um caso legítimo: usuário sem vínculo não enxerga fila. */
  lojas?: Record<string, PapelUsuario>;
  email?: string;
  nome?: string;
  senha?: string;
  ativo?: boolean;
}

// Cria um usuário do dashboard e devolve a senha em claro -- só o teste a vê. O hash sai
// da mesma rotina de produção (auth/senha.ts), nunca de uma versão "de teste": um atalho
// aqui faria a suíte inteira passar por um caminho que ninguém usa de verdade.
//
// scrypt custa ~150 ms por chamada de propósito (é o ponto dele), e a suíte trunca o
// banco entre casos -- semear um usuário por caso pagaria isso dezenas de vezes. O hash
// é memorizado por senha dentro do processo: a rotina de produção roda de verdade na
// primeira vez, e o que se reaproveita depois é o resultado dela, não um atalho.
const hashesMemorizados = new Map<string, string>();

async function hashMemorizado(senha: string): Promise<string> {
  const memorizado = hashesMemorizados.get(senha);
  if (memorizado !== undefined) {
    return memorizado;
  }
  const hash = await geraHashSenha(senha);
  hashesMemorizados.set(senha, hash);
  return hash;
}

export const SENHA_PADRAO_TESTE = "senha-de-teste-do-gerente";

export async function semeiaUsuario(opcoes: SemeiaUsuarioOpcoes): Promise<UsuarioSemeado> {
  const sufixo = randomUUID().slice(0, 8);
  const email = (opcoes.email ?? `gerente-${sufixo}@teste.local`).toLowerCase();
  const senha = opcoes.senha ?? SENHA_PADRAO_TESTE;

  const usuario = await prismaTeste.usuario.create({
    data: {
      tenantId: opcoes.tenantId,
      email,
      nome: opcoes.nome ?? `Gerente ${sufixo}`,
      senhaHash: await hashMemorizado(senha),
      ativo: opcoes.ativo ?? true,
      lojas: {
        create: Object.entries(opcoes.lojas ?? {}).map(([lojaId, papel]) => ({ lojaId, papel })),
      },
    },
  });

  return {
    usuarioId: usuario.id,
    tenantId: usuario.tenantId,
    email,
    senha,
    nome: usuario.nome,
  };
}
