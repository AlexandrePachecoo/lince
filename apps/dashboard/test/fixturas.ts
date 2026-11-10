import type {
  CameraMetrica,
  Evento,
  Fila,
  MetricaFalsoPositivo,
  Sessao,
} from "../src/api/tipos.js";

// Os documentos que a suíte usa no lugar da API. Ficam num arquivo só porque
// contrato.test.ts valida **estes mesmos objetos** contra os JSON Schemas de
// @lince/shared: se cada teste montasse o seu, a validação cobriria os que alguém
// lembrou de validar, que é justamente o conjunto que não precisa de proteção.

export function evento(sobrescreve: Partial<Evento> = {}): Evento {
  return {
    event_id: "8f14e45f-ceea-467a-9a1e-4b7f2c9d1a10",
    tenant_id: "tenant-dev",
    store_id: "loja-centro",
    camera_id: "cam3",
    occurred_at: "2026-08-29T14:00:00.000Z",
    reported_at: "2026-08-29T14:00:02.500Z",
    received_at: "2026-08-29T14:00:04.000Z",
    source: "rule",
    rule: { id: "saida-sem-passar-no-caixa", version: 3 },
    gesture: null,
    versions: { agent: "0.1.0", model: "yolox_s-abc123", config: "v7" },
    clip_state: "disponivel",
    clip_error: null,
    clip: { status: "ok", duration_s: 15, size_bytes: 1_200_000 },
    triagem: null,
    ...sobrescreve,
  };
}

/** Um evento de gesto como a regra da bolsa manda (common.v1.json, `gesture`). */
export function eventoDeGesto(sobrescreve: Partial<Evento> = {}): Evento {
  return evento({
    source: "gesture",
    rule: null,
    gesture: {
      method: { id: "regra-bolsa", version: "1" },
      track_id: 7,
      hand: "right",
      started_s_before: 1.25,
      score: null,
    },
    clip: { status: "ok", duration_s: 15, size_bytes: 1_200_000, pre_roll_s: 5.0 },
    ...sobrescreve,
  });
}

export function fila(eventos: Evento[], proximoCursor: string | null = null): Fila {
  return { schema_version: 1, eventos, proximo_cursor: proximoCursor };
}

export function sessao(sobrescreve: Partial<Sessao> = {}): Sessao {
  return {
    schema_version: 1,
    token: "token-de-teste-opaco",
    expires_in_s: 43_200,
    usuario: {
      id: "usr-1",
      tenant_id: "tenant-dev",
      email: "gerente@loja-dev.local",
      nome: "Ana Gerente",
      ativo: true,
      lojas: [{ store_id: "loja-centro", papel: "gerente" }],
    },
    ...sobrescreve,
  };
}

export function cameraMetrica(sobrescreve: Partial<CameraMetrica> = {}): CameraMetrica {
  return {
    store_id: "loja-centro",
    camera_id: "cam3",
    source: "rule",
    eventos: 10,
    confirmado: 2,
    falso_positivo: 5,
    inconclusivo: 1,
    pendentes: 2,
    falso_positivo_por_dia: 0.71,
    pior_dia: { dia: "2026-08-27", falso_positivo: 4 },
    acima_do_limite: true,
    ...sobrescreve,
  };
}

export function metrica(
  cameras: CameraMetrica[] = [cameraMetrica()],
  dias = 7,
): MetricaFalsoPositivo {
  return {
    schema_version: 1,
    periodo: { desde: "2026-08-23", ate: "2026-08-29", dias, fuso: "America/Sao_Paulo" },
    limite_diario: 3,
    cameras,
  };
}
