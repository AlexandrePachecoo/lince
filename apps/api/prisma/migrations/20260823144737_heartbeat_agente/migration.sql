-- AlterTable
ALTER TABLE "agente" ADD COLUMN     "heartbeat" JSONB,
ADD COLUMN     "heartbeat_em" TIMESTAMP(3);
