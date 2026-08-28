-- CreateEnum
CREATE TYPE "papel_usuario" AS ENUM ('admin', 'gerente', 'operador');

-- CreateEnum
CREATE TYPE "decisao_triagem" AS ENUM ('confirmado', 'falso_positivo', 'inconclusivo');

-- CreateEnum
CREATE TYPE "auditoria_acao" AS ENUM ('clipe_url_emitida');

-- CreateTable
CREATE TABLE "usuario" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "email" TEXT NOT NULL,
    "nome" TEXT NOT NULL,
    "senha_hash" TEXT NOT NULL,
    "ativo" BOOLEAN NOT NULL DEFAULT true,
    "token_versao" INTEGER NOT NULL DEFAULT 0,
    "criado_em" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "atualizado_em" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "usuario_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "usuario_loja" (
    "usuario_id" TEXT NOT NULL,
    "loja_id" TEXT NOT NULL,
    "papel" "papel_usuario" NOT NULL,
    "criado_em" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "usuario_loja_pkey" PRIMARY KEY ("usuario_id","loja_id")
);

-- CreateTable
CREATE TABLE "triagem" (
    "id" TEXT NOT NULL,
    "seq" SERIAL NOT NULL,
    "event_id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "usuario_id" TEXT NOT NULL,
    "decisao" "decisao_triagem" NOT NULL,
    "observacao" TEXT,
    "criado_em" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "triagem_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "auditoria" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "acao" "auditoria_acao" NOT NULL,
    "usuario_id" TEXT NOT NULL,
    "usuario_email" TEXT NOT NULL,
    "event_id" TEXT,
    "object_key" TEXT,
    "ip" TEXT,
    "user_agent" TEXT,
    "criado_em" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "auditoria_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "usuario_email_key" ON "usuario"("email");

-- CreateIndex
CREATE INDEX "usuario_tenant_id_idx" ON "usuario"("tenant_id");

-- CreateIndex
CREATE INDEX "usuario_loja_loja_id_idx" ON "usuario_loja"("loja_id");

-- CreateIndex
CREATE UNIQUE INDEX "triagem_seq_key" ON "triagem"("seq");

-- CreateIndex
CREATE INDEX "triagem_event_id_seq_idx" ON "triagem"("event_id", "seq");

-- CreateIndex
CREATE INDEX "triagem_tenant_id_criado_em_idx" ON "triagem"("tenant_id", "criado_em");

-- CreateIndex
CREATE INDEX "auditoria_tenant_id_criado_em_idx" ON "auditoria"("tenant_id", "criado_em");

-- CreateIndex
CREATE INDEX "auditoria_event_id_idx" ON "auditoria"("event_id");

-- AddForeignKey
ALTER TABLE "usuario" ADD CONSTRAINT "usuario_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenant"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "usuario_loja" ADD CONSTRAINT "usuario_loja_usuario_id_fkey" FOREIGN KEY ("usuario_id") REFERENCES "usuario"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "usuario_loja" ADD CONSTRAINT "usuario_loja_loja_id_fkey" FOREIGN KEY ("loja_id") REFERENCES "loja"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "triagem" ADD CONSTRAINT "triagem_event_id_fkey" FOREIGN KEY ("event_id") REFERENCES "evento"("event_id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "triagem" ADD CONSTRAINT "triagem_usuario_id_fkey" FOREIGN KEY ("usuario_id") REFERENCES "usuario"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
