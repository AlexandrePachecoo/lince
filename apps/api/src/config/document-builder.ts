import { createHash } from "node:crypto";
import type { Camera, Loja } from "@prisma/client";
import { canonicalize } from "./canonical-json.js";

export type LojaComCameras = Loja & { cameras: Camera[] };

export interface CameraDocumento {
  camera_id: string;
  url: string;
  detect: boolean;
  decode?: unknown;
  supervision?: unknown;
  clip?: unknown;
  rules?: unknown;
}

export interface ConfigDocumento {
  schema_version: 1;
  config_version: string;
  tenant_id: string;
  store_id: string;
  cameras: CameraDocumento[];
  detection?: unknown;
  tracking?: unknown;
}

// Monta o documento de GET /v1/agents/config (packages/shared/schemas/config.v1.json)
// a partir de linhas já carregadas do Prisma. Puro: não recebe PrismaClient, não faz
// I/O -- é o que permite testar sem banco (mesmo espírito de outbox/policy.py no
// agente, puro "por desenho, não por elegância").
export function montaDocumento(loja: LojaComCameras): ConfigDocumento {
  // Ordem estável por camera_id, independente da ordem em que o Postgres devolveu as
  // linhas -- sustenta o ETag: mesmo conteúdo, ordens de leitura diferentes, mesmo hash.
  const cameras = [...loja.cameras]
    .sort((a, b) => (a.cameraId < b.cameraId ? -1 : a.cameraId > b.cameraId ? 1 : 0))
    .map(montaCameraDocumento);

  const conteudo: Omit<ConfigDocumento, "config_version"> = {
    schema_version: 1,
    tenant_id: loja.tenantId,
    store_id: loja.id,
    cameras,
  };
  // Bloco de nível-loja ausente (null no banco) vira campo ausente no documento, não
  // `null` explícito -- o agente trata ausência como "usa o default dele"
  // (config_loader.py), e um `detection: null` seria um valor que o schema recusa
  // (o campo, se presente, é um objeto).
  if (loja.detection !== null) {
    conteudo.detection = loja.detection;
  }
  if (loja.tracking !== null) {
    conteudo.tracking = loja.tracking;
  }

  return {
    ...conteudo,
    config_version: calculaConfigVersion(conteudo),
  };
}

function montaCameraDocumento(camera: Camera): CameraDocumento {
  const doc: CameraDocumento = {
    camera_id: camera.cameraId,
    url: camera.url,
    detect: camera.detect,
  };
  if (camera.decode !== null) doc.decode = camera.decode;
  if (camera.supervision !== null) doc.supervision = camera.supervision;
  if (camera.clip !== null) doc.clip = camera.clip;
  if (camera.rules !== null) doc.rules = camera.rules;
  return doc;
}

// Hash do conteúdo (tudo exceto config_version, que não pode hashear a si mesmo).
// Mesmo conteúdo -> mesmo hash (ETag estável, §5.4); qualquer campo mudando -> hash
// diferente. Não é criptografia -- é só um identificador opaco de conteúdo, então
// SHA-256 é overkill de colisão de propósito (barato, sem biblioteca extra).
function calculaConfigVersion(conteudo: Omit<ConfigDocumento, "config_version">): string {
  return createHash("sha256").update(canonicalize(conteudo)).digest("hex");
}
