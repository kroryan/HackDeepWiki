# ADR-001: modelo de despliegue soportado

- Estado: aceptado
- Fecha: 2026-07-26

## Decisión

HackDeepWiki es una aplicación local-first para un usuario, un proceso y
almacenamiento local. El ejecutable empaquetado enlaza exclusivamente en
loopback de forma predeterminada.

Se admite una LAN confiable solo si se cumplen simultáneamente estas
condiciones:

- `HACKDEEPWIKI_DEPLOYMENT_PROFILE=trusted-lan`;
- `HACKDEEPWIKI_AUTH_MODE=true`;
- un código de autorización de al menos 16 caracteres;
- TLS terminado por un proxy inverso;
- una allowlist explícita de orígenes y una política de egress adecuada.

No se soportan todavía Internet público, multiusuario, varios workers del
backend ni almacenamiento compartido por varios procesos.

## Motivos

SQLite/WAL, el worker embebido, el proceso OpenCode, el dashboard Engraphis y
varios coordinadores son deliberadamente de un solo proceso. Esta arquitectura
reduce requisitos y hace portable el producto, pero no proporciona identidad
por usuario, autorización por repositorio, cuotas ni coordinación distribuida.

El servidor valida el perfil al arrancar. Un bind no-loopback sin autenticación
fuerte falla cerrado, salvo que un futuro ADR defina otro modelo completo.

## Consecuencias

- Un único ejecutable sigue siendo la unidad de despliegue.
- No se introducen microservicios, Redis, Postgres o Kubernetes por defecto.
- Toda nueva ruta HTTP o WebSocket hereda la frontera global de autenticación.
- Cualquier propuesta multiusuario necesita un ADR nuevo que cubra identidad,
  aislamiento de secretos/procesos/repositorios, auditoría y límites.
