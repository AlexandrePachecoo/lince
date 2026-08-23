-- CreateEnum
CREATE TYPE "evento_origem" AS ENUM ('rule', 'manual');

-- CreateEnum
CREATE TYPE "clipe_estado" AS ENUM ('pendente', 'disponivel', 'indisponivel');

-- CreateTable
CREATE TABLE "evento" (
    "event_id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "loja_id" TEXT NOT NULL,
    "camera_id" TEXT NOT NULL,
    "agente_id" TEXT,
    "ocorrido_em" TIMESTAMP(3) NOT NULL,
    "reportado_em" TIMESTAMP(3) NOT NULL,
    "recebido_em" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "source" "evento_origem" NOT NULL,
    "regra_id" TEXT,
    "regra_versao" INTEGER,
    "versions" JSONB NOT NULL,
    "clip" JSONB NOT NULL,
    "clipe_estado" "clipe_estado" NOT NULL DEFAULT 'pendente',
    "clipe_object_key" TEXT,
    "clipe_erro" TEXT,
    "clipe_resolvido_em" TIMESTAMP(3),
    "atualizado_em" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "evento_pkey" PRIMARY KEY ("event_id")
);

-- CreateIndex
CREATE INDEX "evento_tenant_id_ocorrido_em_idx" ON "evento"("tenant_id", "ocorrido_em");

-- CreateIndex
CREATE INDEX "evento_tenant_id_loja_id_camera_id_ocorrido_em_idx" ON "evento"("tenant_id", "loja_id", "camera_id", "ocorrido_em");

-- CreateIndex
CREATE INDEX "evento_tenant_id_clipe_estado_idx" ON "evento"("tenant_id", "clipe_estado");

-- AddForeignKey
ALTER TABLE "evento" ADD CONSTRAINT "evento_loja_id_fkey" FOREIGN KEY ("loja_id") REFERENCES "loja"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "evento" ADD CONSTRAINT "evento_agente_id_fkey" FOREIGN KEY ("agente_id") REFERENCES "agente"("id") ON DELETE SET NULL ON UPDATE CASCADE;
