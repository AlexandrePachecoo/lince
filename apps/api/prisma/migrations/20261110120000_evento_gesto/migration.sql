-- AlterEnum
ALTER TYPE "evento_origem" ADD VALUE 'gesture';

-- AlterTable
ALTER TABLE "evento" ADD COLUMN     "gesto" JSONB,
ADD COLUMN     "gesto_metodo" TEXT,
ADD COLUMN     "gesto_metodo_versao" TEXT;
