-- CreateTable
CREATE TABLE "tenant" (
    "id" TEXT NOT NULL,
    "nome" TEXT NOT NULL,
    "criado_em" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "tenant_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "loja" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "nome" TEXT NOT NULL,
    "detection" JSONB,
    "tracking" JSONB,
    "criado_em" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "atualizado_em" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "loja_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "camera" (
    "id" TEXT NOT NULL,
    "camera_id" TEXT NOT NULL,
    "loja_id" TEXT NOT NULL,
    "url" TEXT NOT NULL,
    "detect" BOOLEAN NOT NULL DEFAULT true,
    "decode" JSONB,
    "supervision" JSONB,
    "clip" JSONB,
    "rules" JSONB,
    "criado_em" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "atualizado_em" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "camera_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "agente" (
    "id" TEXT NOT NULL,
    "loja_id" TEXT NOT NULL,
    "token_hash" TEXT NOT NULL,
    "token_prefix" TEXT NOT NULL,
    "ativo" BOOLEAN NOT NULL DEFAULT true,
    "criado_em" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "ultimo_uso_em" TIMESTAMP(3),

    CONSTRAINT "agente_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "loja_tenant_id_idx" ON "loja"("tenant_id");

-- CreateIndex
CREATE INDEX "camera_loja_id_idx" ON "camera"("loja_id");

-- CreateIndex
CREATE UNIQUE INDEX "camera_loja_id_camera_id_key" ON "camera"("loja_id", "camera_id");

-- CreateIndex
CREATE UNIQUE INDEX "agente_token_hash_key" ON "agente"("token_hash");

-- CreateIndex
CREATE INDEX "agente_loja_id_idx" ON "agente"("loja_id");

-- AddForeignKey
ALTER TABLE "loja" ADD CONSTRAINT "loja_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenant"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "camera" ADD CONSTRAINT "camera_loja_id_fkey" FOREIGN KEY ("loja_id") REFERENCES "loja"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "agente" ADD CONSTRAINT "agente_loja_id_fkey" FOREIGN KEY ("loja_id") REFERENCES "loja"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
