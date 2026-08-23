import { AwsClient } from "aws4fetch";

// Emissão das URLs pré-assinadas dos clipes (§4.4). O vídeo não passa pela API em
// nenhum momento: a API assina, o agente faz o PUT direto no bucket e o dashboard
// fará o GET direto por URL assinada de expiração curta (NFR-7).
//
// `aws4fetch` em vez do SDK da AWS pelo mesmo motivo que o agente usa `urllib` em vez
// de `httpx`: o que se precisa aqui é assinar uma URL, e o SDK traz um cliente S3
// inteiro -- retry, paginação, streaming de multipart -- para isso. Assinar é a única
// operação, é SigV4 puro, e o teste contra o MinIO cobre o resultado de ponta a ponta.
//
// Path-style (`{endpoint}/{bucket}/{chave}`) serve ao R2 e ao MinIO, então não há um
// caso especial por ambiente: o que muda entre desenvolvimento e produção é só o
// endpoint e a credencial.

export interface ConfigArmazenamento {
  endpoint: string;
  region: string;
  accessKeyId: string;
  secretAccessKey: string;
  bucket: string;
}

export class ArmazenamentoClipes {
  private readonly aws: AwsClient;
  private readonly base: string;

  constructor(private readonly config: ConfigArmazenamento) {
    this.aws = new AwsClient({
      accessKeyId: config.accessKeyId,
      secretAccessKey: config.secretAccessKey,
      service: "s3",
      region: config.region,
    });
    // Sem barra no fim: as chaves já começam sem barra e `${base}/${chave}` produziria
    // `//clipes/...`. Uma barra dobrada não é erro de assinatura -- ela vira parte da
    // chave, e o objeto pousa num caminho que ninguém procura depois.
    this.base = config.endpoint.replace(/\/+$/, "");
  }

  /** URL para o `PUT` do clipe pelo agente. */
  async urlDeUpload(chave: string, expiraEmS: number): Promise<string> {
    return this.assina(chave, "PUT", expiraEmS);
  }

  /** URL para leitura do clipe (NFR-7). O dashboard ainda não existe; quem usa hoje é
   * o teste de ponta a ponta, que confere que os bytes do agente chegaram inteiros. */
  async urlDeLeitura(chave: string, expiraEmS: number): Promise<string> {
    return this.assina(chave, "GET", expiraEmS);
  }

  private async assina(chave: string, metodo: string, expiraEmS: number): Promise<string> {
    if (!Number.isFinite(expiraEmS) || expiraEmS <= 0) {
      throw new Error(`expiração inválida para URL pré-assinada: ${expiraEmS}`);
    }
    const url = new URL(`${this.base}/${this.config.bucket}/${chave}`);
    url.searchParams.set("X-Amz-Expires", String(Math.floor(expiraEmS)));

    // `signQuery` põe a credencial na query, e não no cabeçalho `Authorization`: é o
    // que permite ao agente fazer o PUT sem conhecer segredo nenhum. Só o `host` entra
    // nos cabeçalhos assinados -- de propósito. Assinar `Content-Type` obrigaria o
    // agente a mandar exatamente o mesmo valor, e uma divergência de maiúsculas viraria
    // um 403 do bucket que se parece com credencial errada.
    const assinada = await this.aws.sign(url.toString(), {
      method: metodo,
      aws: { signQuery: true },
    });
    return assinada.url;
  }
}
