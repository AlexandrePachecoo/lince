import webpush from "web-push";

// Gera o par de chaves VAPID da notificação por Web Push (§4.5) já no formato do .env.
//
// Uma vez por ambiente, e guardado: trocar a chave invalida todas as inscrições — cada
// celular inscrito com a chave antiga deixa de receber até o gerente reativar a
// notificação no dashboard. Em produção, a privada vai para o cofre de segredos, nunca
// para o repositório.

const { publicKey, privateKey } = webpush.generateVAPIDKeys();
console.log(`VAPID_PUBLIC_KEY=${publicKey}`);
console.log(`VAPID_PRIVATE_KEY=${privateKey}`);
console.log("VAPID_SUBJECT=mailto:suporte@lince.local");
