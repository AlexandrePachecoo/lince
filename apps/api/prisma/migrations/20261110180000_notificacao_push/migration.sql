-- CreateEnum
CREATE TYPE "alerta_decisao" AS ENUM ('notificado', 'agrupado', 'teto', 'atrasado');

-- CreateEnum
CREATE TYPE "envio_status" AS ENUM ('enviado', 'falhou', 'expirada');

-- CreateTable
CREATE TABLE "alerta" (
    "event_id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "loja_id" TEXT NOT NULL,
    "camera_id" TEXT NOT NULL,
    "source" "evento_origem" NOT NULL,
    "decisao" "alerta_decisao" NOT NULL,
    "criado_em" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "alerta_pkey" PRIMARY KEY ("event_id")
);

-- CreateTable
CREATE TABLE "inscricao_push" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "usuario_id" TEXT NOT NULL,
    "endpoint" TEXT NOT NULL,
    "p256dh" TEXT NOT NULL,
    "auth" TEXT NOT NULL,
    "criado_em" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "inscricao_push_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "envio_alerta" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "event_id" TEXT NOT NULL,
    "usuario_id" TEXT NOT NULL,
    "inscricao_id" TEXT NOT NULL,
    "canal" TEXT NOT NULL DEFAULT 'webpush',
    "status" "envio_status" NOT NULL,
    "http_status" INTEGER,
    "erro" TEXT,
    "criado_em" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "envio_alerta_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "alerta_loja_id_camera_id_source_decisao_criado_em_idx" ON "alerta"("loja_id", "camera_id", "source", "decisao", "criado_em");

-- CreateIndex
CREATE INDEX "alerta_loja_id_decisao_criado_em_idx" ON "alerta"("loja_id", "decisao", "criado_em");

-- CreateIndex
CREATE UNIQUE INDEX "inscricao_push_endpoint_key" ON "inscricao_push"("endpoint");

-- CreateIndex
CREATE INDEX "inscricao_push_usuario_id_idx" ON "inscricao_push"("usuario_id");

-- CreateIndex
CREATE INDEX "envio_alerta_event_id_idx" ON "envio_alerta"("event_id");

-- AddForeignKey
ALTER TABLE "alerta" ADD CONSTRAINT "alerta_event_id_fkey" FOREIGN KEY ("event_id") REFERENCES "evento"("event_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "inscricao_push" ADD CONSTRAINT "inscricao_push_usuario_id_fkey" FOREIGN KEY ("usuario_id") REFERENCES "usuario"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "envio_alerta" ADD CONSTRAINT "envio_alerta_event_id_fkey" FOREIGN KEY ("event_id") REFERENCES "evento"("event_id") ON DELETE RESTRICT ON UPDATE CASCADE;
