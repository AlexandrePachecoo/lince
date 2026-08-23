-- CreateTable
CREATE TABLE "token_bootstrap" (
    "id" TEXT NOT NULL,
    "loja_id" TEXT NOT NULL,
    "token_hash" TEXT NOT NULL,
    "token_prefix" TEXT NOT NULL,
    "expira_em" TIMESTAMP(3) NOT NULL,
    "usado_em" TIMESTAMP(3),
    "criado_em" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "token_bootstrap_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "token_bootstrap_token_hash_key" ON "token_bootstrap"("token_hash");

-- CreateIndex
CREATE INDEX "token_bootstrap_loja_id_idx" ON "token_bootstrap"("loja_id");

-- AddForeignKey
ALTER TABLE "token_bootstrap" ADD CONSTRAINT "token_bootstrap_loja_id_fkey" FOREIGN KEY ("loja_id") REFERENCES "loja"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
