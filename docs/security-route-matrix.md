# Matriz de seguridad HTTP y WebSocket

La política se aplica globalmente en `api.security`, no de manera optativa en
cada router. El test `test_every_non_public_openapi_operation_is_denied_without_auth`
recorre OpenAPI y falla si una operación nueva elude esta frontera.

## HTTP

| Política | Rutas | Comportamiento |
|---|---|---|
| Pública | `GET /health`, `GET /health/live` | Solo liveness, sin datos privados |
| Pública | `GET /auth/status` | Indica si se exige autenticación |
| Pública | `POST /auth/validate` | Valida el código con rate limit y devuelve una sesión firmada |
| Autenticada | Todas las demás rutas | Sesión firmada, cookie segura o proxy interno autenticado |
| Privilegiada | OpenCode, imports, scans, borrados, jobs, perfiles, MCP | Misma frontera global; solo se ofrecen en el modelo de usuario único |

Las rutas autenticadas también validan `Origin`. El proxy Next.js usa un token
interno aleatorio que nunca se expone al navegador. `/mcp/token` solo informa
si hay token configurado y nunca devuelve el secreto.

## WebSocket

Todas las rutas validan autorización y origen antes de llamar a `accept()`:

- `/ws/chat`
- `/ws/repo/clone`
- `/ws/website/crawl`
- `/ws/fanwiki/import`
- `/ws/vuln_scan`
- `/ws/web_vuln_scan`
- `/ws/code/chat`
- `/ws/code/events`

El test de contrato enumera el conjunto completo y exige que cada endpoint use
el guard compartido. Una ruta WebSocket nueva debe añadirse deliberadamente al
test y heredar la misma política.

## Egress

Los crawlers y escáneres validan destinos con `api.network_policy`. El perfil
desktop conserva el caso de uso de escaneo local; `trusted-lan` bloquea rangos
privados salvo allowlist explícita para evitar convertir el servidor en proxy
SSRF hacia la LAN.
