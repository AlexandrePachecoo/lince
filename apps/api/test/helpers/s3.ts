import { AwsClient } from "aws4fetch";
import { configArmazenamentoTeste } from "./build-app.js";

// Acesso direto ao bucket de teste, para o que a API não faz: conferir que os bytes
// chegaram e apagar o que o teste criou. Não usa ArmazenamentoClipes de propósito --
// se a asserção usasse o mesmo código que produziu a URL, um erro de endpoint ou de
// bucket passaria despercebido nos dois lados ao mesmo tempo.

const config = configArmazenamentoTeste();

const aws = new AwsClient({
  accessKeyId: config.accessKeyId,
  secretAccessKey: config.secretAccessKey,
  service: "s3",
  region: config.region,
});

function endereco(chave: string): string {
  return `${config.endpoint.replace(/\/+$/, "")}/${config.bucket}/${chave}`;
}

/** URL sem assinatura nenhuma. Serve para provar que o bucket recusa (NFR-7). */
export function urlCrua(chave: string): string {
  return endereco(chave);
}

export interface ObjetoLido {
  status: number;
  bytes: number;
}

export async function leObjeto(chave: string): Promise<ObjetoLido> {
  const resposta = await aws.fetch(endereco(chave), { method: "GET" });
  const corpo = await resposta.arrayBuffer();
  return { status: resposta.status, bytes: corpo.byteLength };
}

export async function apagaObjeto(chave: string): Promise<void> {
  await aws.fetch(endereco(chave), { method: "DELETE" });
}
