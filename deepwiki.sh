#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASE_COMPOSE="${ROOT_DIR}/docker-compose.yml"
OLLAMA_COMPOSE="${ROOT_DIR}/docker-compose.ollama.yml"
LINUX_COMPOSE="${ROOT_DIR}/docker-compose.ollama-linux.yml"
LOCAL_ENV="${ROOT_DIR}/deepwiki.env"
RUNTIME_DIR="${ROOT_DIR}/.deepwiki"

if [[ -f "${LOCAL_ENV}" ]]; then
  # shellcheck disable=SC1090
  source "${LOCAL_ENV}"
fi

OLLAMA_ENDPOINT="${OLLAMA_ENDPOINT:-http://127.0.0.1:11434}"
OLLAMA_MODEL="${OLLAMA_MODEL:-}"
OLLAMA_EMBED_MODEL="${OLLAMA_EMBED_MODEL:-}"
OLLAMA_EMBED_BATCH_SIZE="${OLLAMA_EMBED_BATCH_SIZE:-32}"
OLLAMA_REQUEST_TIMEOUT="${OLLAMA_REQUEST_TIMEOUT:-1800}"
OLLAMA_HEALTH_TIMEOUT="${OLLAMA_HEALTH_TIMEOUT:-60}"
GITHUB_TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
DEEPWIKI_API_PORT="${DEEPWIKI_API_PORT:-8001}"
DEEPWIKI_PROJECT_NAME="${DEEPWIKI_PROJECT_NAME:-deepwiki-open-local}"
DEEPWIKI_NETWORK_MODE="${DEEPWIKI_NETWORK_MODE:-auto}"
EFFECTIVE_NETWORK_MODE="${DEEPWIKI_NETWORK_MODE}"
OLLAMA_CONTAINER_ENDPOINT="${OLLAMA_ENDPOINT}"

COMMAND=""
BUILD=true
FOLLOW=false

usage() {
  cat <<'EOF'
Uso:
  ./deepwiki.sh <comando> [opciones]

Comandos:
  setup          Comprueba Ollama y construye la imagen
  up             Prepara la configuración y arranca DeepWiki
  down           Para y elimina solo los contenedores de DeepWiki
  restart        Recrea DeepWiki con la configuración actual
  status         Muestra el estado de los contenedores
  logs           Muestra los últimos logs (usa -f para seguirlos)
  health         Comprueba Ollama, API y web
  doctor         Diagnóstico de dependencias, configuración y puertos
  models         Lista los modelos disponibles en Ollama
  pull-models    Descarga los modelos configurados
  test           Valida script, Compose y configuración derivada
  config         Muestra la configuración efectiva

Opciones:
  -h, --help
  -ollama-endpoint, --ollama-endpoint URL
                  Endpoint de Ollama (por defecto http://127.0.0.1:11434)
  -ollama-model, --ollama-model, --model MODELO
                  Opcional: fija el modelo predeterminado entre los descubiertos
  --embed-model MODELO
                  Opcional: fija el embedder entre los descubiertos
  --embed-batch-size N
                  Textos por petición de embeddings (por defecto 32)
  --ollama-timeout SEGUNDOS
                  Timeout por tanda de embeddings (por defecto 1800)
  --ollama-health-timeout SEGUNDOS
                  Espera del chequeo remoto previo (por defecto 60)
  --github-token-file RUTA
                  Lee un token GitHub desde un archivo (evita el historial)
  --api-port PORT Puerto de la API (por defecto 8001)
  --network MODO  auto, host o bridge (por defecto auto)
  --no-build      No reconstruir la imagen al ejecutar up/restart
  -f, --follow    Seguir logs

Variables equivalentes:
  OLLAMA_ENDPOINT, OLLAMA_MODEL, OLLAMA_EMBED_MODEL,
  OLLAMA_EMBED_BATCH_SIZE, OLLAMA_REQUEST_TIMEOUT, OLLAMA_HEALTH_TIMEOUT,
  GITHUB_TOKEN,
  DEEPWIKI_API_PORT, DEEPWIKI_PROJECT_NAME, DEEPWIKI_NETWORK_MODE

Ejemplos:
  ./deepwiki.sh setup
  ./deepwiki.sh up
  ./deepwiki.sh up --no-build
  ./deepwiki.sh up --ollama-endpoint http://100.94.16.58:11434
  ./deepwiki.sh logs -f

Los valores persistentes pueden guardarse en deepwiki.env tomando como base
deepwiki.env.example. `down` no detiene Ollama y no hay política de autoarranque.
EOF
}

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

info() {
  printf '==> %s\n' "$*"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Falta '$1'. Instálalo y vuelve a intentarlo."
}

normalize_endpoint() {
  OLLAMA_ENDPOINT="${OLLAMA_ENDPOINT%/}"
  OLLAMA_ENDPOINT="${OLLAMA_ENDPOINT%/api}"
  [[ "${OLLAMA_ENDPOINT}" =~ ^https?:// ]] ||
    die "El endpoint de Ollama debe empezar por http:// o https://"
}

validate_port() {
  [[ "${DEEPWIKI_API_PORT}" =~ ^[0-9]+$ ]] ||
    die "El puerto de API debe ser numérico"
  ((DEEPWIKI_API_PORT >= 1 && DEEPWIKI_API_PORT <= 65535)) ||
    die "El puerto de API debe estar entre 1 y 65535"
}

select_network_mode() {
  local docker_os=""
  docker_os="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || true)"

  case "${DEEPWIKI_NETWORK_MODE}" in
    auto)
      if [[ "$(uname -s)" == "Linux" ]] &&
        [[ -z "${WSL_DISTRO_NAME:-}" ]] &&
        [[ "${docker_os}" != *"Docker Desktop"* ]]; then
        EFFECTIVE_NETWORK_MODE="host"
      else
        EFFECTIVE_NETWORK_MODE="bridge"
      fi
      ;;
    host|bridge)
      EFFECTIVE_NETWORK_MODE="${DEEPWIKI_NETWORK_MODE}"
      ;;
    *)
      die "--network debe ser auto, host o bridge"
      ;;
  esac

  OLLAMA_CONTAINER_ENDPOINT="${OLLAMA_ENDPOINT}"
  if [[ "${EFFECTIVE_NETWORK_MODE}" == "bridge" ]] &&
    [[ "${OLLAMA_ENDPOINT}" =~ ^(https?://)(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?$ ]]; then
    OLLAMA_CONTAINER_ENDPOINT="${BASH_REMATCH[1]}host.docker.internal${BASH_REMATCH[3]:-}"
  fi
}

compose() {
  local compose_files=(
    --project-name "${DEEPWIKI_PROJECT_NAME}"
    --file "${BASE_COMPOSE}"
    --file "${OLLAMA_COMPOSE}"
  )
  if [[ "${EFFECTIVE_NETWORK_MODE}" == "host" ]]; then
    compose_files+=(--file "${LINUX_COMPOSE}")
  fi

  PORT="${DEEPWIKI_API_PORT}" \
  OLLAMA_CONTAINER_ENDPOINT="${OLLAMA_CONTAINER_ENDPOINT}" \
  OLLAMA_MODEL="${OLLAMA_MODEL}" \
  OLLAMA_EMBED_MODEL="${OLLAMA_EMBED_MODEL}" \
  OLLAMA_EMBED_BATCH_SIZE="${OLLAMA_EMBED_BATCH_SIZE}" \
  OLLAMA_REQUEST_TIMEOUT="${OLLAMA_REQUEST_TIMEOUT}" \
  GITHUB_TOKEN="${GITHUB_TOKEN}" \
    docker compose "${compose_files[@]}" "$@"
}

check_ollama() {
  require_command curl
  curl --fail --silent --show-error \
    --max-time "${OLLAMA_HEALTH_TIMEOUT}" \
    "${OLLAMA_ENDPOINT}/api/version" >/dev/null ||
    die "No se puede acceder a Ollama en ${OLLAMA_ENDPOINT}"
}

ollama_cli() {
  require_command ollama
  OLLAMA_HOST="${OLLAMA_ENDPOINT}" ollama "$@"
}

model_exists() {
  ollama_cli show "$1" >/dev/null 2>&1
}

pull_models() {
  check_ollama
  [[ -n "${OLLAMA_EMBED_MODEL}" || -n "${OLLAMA_MODEL}" ]] ||
    die "Indica --ollama-model y/o --embed-model para descargar modelos"
  for model in "${OLLAMA_EMBED_MODEL}" "${OLLAMA_MODEL}"; do
    [[ -n "${model}" ]] || continue
    if model_exists "${model}"; then
      info "Modelo disponible: ${model}"
    else
      info "Descargando modelo: ${model}"
      ollama_cli pull "${model}"
    fi
  done
}

check_dependencies() {
  require_command docker
  docker compose version >/dev/null 2>&1 ||
    die "Se necesita Docker Compose v2"
  docker info >/dev/null 2>&1 ||
    die "Docker no está accesible para este usuario"
  require_command curl
  select_network_mode
}

show_config() {
  local selected_model="${OLLAMA_MODEL:-automático desde Ollama}"
  local selected_embed_model="${OLLAMA_EMBED_MODEL:-automático desde Ollama}"
  local github_auth="no configurado"
  [[ -n "${GITHUB_TOKEN}" ]] && github_auth="configurado"
  cat <<EOF
Proyecto Compose : ${DEEPWIKI_PROJECT_NAME}
Web              : http://localhost:3000
API              : http://localhost:${DEEPWIKI_API_PORT}
Ollama           : ${OLLAMA_ENDPOINT}
Modelo           : ${selected_model}
Embeddings       : ${selected_embed_model}
Tanda embeddings : ${OLLAMA_EMBED_BATCH_SIZE}
Timeout Ollama   : ${OLLAMA_REQUEST_TIMEOUT}s (solo embeddings)
Chequeo Ollama   : ${OLLAMA_HEALTH_TIMEOUT}s
Token GitHub      : ${github_auth}
Red Docker        : ${EFFECTIVE_NETWORK_MODE}
Config local     : ${RUNTIME_DIR}/config
Autoarranque     : no
EOF
}

health() {
  local failed=0
  if curl --fail --silent --max-time "${OLLAMA_HEALTH_TIMEOUT}" \
    "${OLLAMA_ENDPOINT}/api/version" >/dev/null; then
    printf 'OK    Ollama  %s\n' "${OLLAMA_ENDPOINT}"
  else
    printf 'ERROR Ollama  %s\n' "${OLLAMA_ENDPOINT}"
    failed=1
  fi
  if curl --fail --silent --max-time 5 \
    "http://127.0.0.1:${DEEPWIKI_API_PORT}/health" >/dev/null; then
    printf 'OK    API     http://localhost:%s\n' "${DEEPWIKI_API_PORT}"
  else
    printf 'ERROR API     http://localhost:%s\n' "${DEEPWIKI_API_PORT}"
    failed=1
  fi
  if curl --fail --silent --max-time 5 "http://127.0.0.1:3000" >/dev/null; then
    printf 'OK    Web     http://localhost:3000\n'
  else
    printf 'ERROR Web     http://localhost:3000\n'
    failed=1
  fi
  return "${failed}"
}

doctor() {
  local failed=0
  show_config
  printf '\n'
  for command in docker curl; do
    if command -v "${command}" >/dev/null 2>&1; then
      printf 'OK    comando %s\n' "${command}"
    else
      printf 'ERROR falta %s\n' "${command}"
      failed=1
    fi
  done
  if docker info >/dev/null 2>&1; then
    printf 'OK    Docker accesible\n'
    select_network_mode
    printf 'OK    red Docker %s\n' "${EFFECTIVE_NETWORK_MODE}"
  else
    printf 'ERROR Docker no accesible\n'
    failed=1
  fi
  if curl --fail --silent --max-time "${OLLAMA_HEALTH_TIMEOUT}" \
    "${OLLAMA_ENDPOINT}/api/version" >/dev/null; then
    printf 'OK    Ollama accesible\n'
  else
    printf 'ERROR Ollama no accesible\n'
    failed=1
  fi
  if curl --fail --silent --max-time "${OLLAMA_HEALTH_TIMEOUT}" \
    "${OLLAMA_ENDPOINT}/api/tags" >/dev/null; then
    printf 'OK    catálogo de modelos accesible\n'
  else
    printf 'ERROR catálogo de modelos no accesible\n'
    failed=1
  fi
  if [[ -n "${GITHUB_TOKEN}" ]]; then
    printf 'OK    token GitHub configurado\n'
  else
    printf 'AVISO token GitHub no configurado (límite anónimo)\n'
  fi
  return "${failed}"
}

while (($#)); do
  case "$1" in
    setup|up|down|restart|status|logs|health|doctor|models|pull-models|test|config)
      [[ -z "${COMMAND}" ]] || die "Solo se admite un comando"
      COMMAND="$1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -ollama-endpoint|--ollama-endpoint)
      (($# >= 2)) || die "Falta la URL de Ollama"
      OLLAMA_ENDPOINT="$2"
      shift 2
      ;;
    -ollama-model|--ollama-model|--model)
      (($# >= 2)) || die "Falta el nombre del modelo"
      OLLAMA_MODEL="$2"
      shift 2
      ;;
    --embed-model)
      (($# >= 2)) || die "Falta el modelo de embeddings"
      OLLAMA_EMBED_MODEL="$2"
      shift 2
      ;;
    --embed-batch-size)
      (($# >= 2)) || die "Falta el tamaño de tanda"
      OLLAMA_EMBED_BATCH_SIZE="$2"
      shift 2
      ;;
    --ollama-timeout)
      (($# >= 2)) || die "Falta el timeout de Ollama"
      OLLAMA_REQUEST_TIMEOUT="$2"
      shift 2
      ;;
    --ollama-health-timeout)
      (($# >= 2)) || die "Falta el timeout del chequeo de Ollama"
      OLLAMA_HEALTH_TIMEOUT="$2"
      shift 2
      ;;
    --github-token-file)
      (($# >= 2)) || die "Falta la ruta del archivo de token"
      [[ -r "$2" ]] || die "No se puede leer el archivo de token: $2"
      IFS= read -r GITHUB_TOKEN < "$2"
      [[ -n "${GITHUB_TOKEN}" ]] || die "El archivo de token está vacío"
      shift 2
      ;;
    --api-port)
      (($# >= 2)) || die "Falta el puerto"
      DEEPWIKI_API_PORT="$2"
      shift 2
      ;;
    --network)
      (($# >= 2)) || die "Falta el modo de red"
      DEEPWIKI_NETWORK_MODE="$2"
      shift 2
      ;;
    --no-build)
      BUILD=false
      shift
      ;;
    -f|--follow)
      FOLLOW=true
      shift
      ;;
    *)
      die "Opción desconocida: $1 (usa -h para ver la ayuda)"
      ;;
  esac
done

[[ -n "${COMMAND}" ]] || {
  usage
  exit 0
}
normalize_endpoint
validate_port
[[ "${OLLAMA_EMBED_BATCH_SIZE}" =~ ^[1-9][0-9]*$ ]] ||
  die "El tamaño de tanda debe ser un entero positivo"
[[ "${OLLAMA_REQUEST_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] ||
  die "El timeout de Ollama debe ser un entero positivo"
[[ "${OLLAMA_HEALTH_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] ||
  die "El timeout del chequeo de Ollama debe ser un entero positivo"
cd "${ROOT_DIR}"

case "${COMMAND}" in
  setup)
    check_dependencies
    check_ollama
    mkdir -p "${RUNTIME_DIR}"
    info "Construyendo DeepWiki"
    compose build
    info "Setup completado"
    show_config
    ;;
  up)
    check_dependencies
    check_ollama
    mkdir -p "${RUNTIME_DIR}"
    info "Arrancando DeepWiki"
    if [[ "${BUILD}" == true ]]; then
      compose up --detach --build --force-recreate --wait --wait-timeout 120
    else
      compose up --detach --force-recreate --wait --wait-timeout 120
    fi
    info "DeepWiki arrancado y listo"
    show_config
    ;;
  down)
    check_dependencies
    info "Deteniendo DeepWiki (Ollama seguirá activo)"
    compose down
    ;;
  restart)
    check_dependencies
    check_ollama
    mkdir -p "${RUNTIME_DIR}"
    compose down
    if [[ "${BUILD}" == true ]]; then
      compose up --detach --build --wait --wait-timeout 120
    else
      compose up --detach --wait --wait-timeout 120
    fi
    show_config
    ;;
  status)
    check_dependencies
    compose ps
    ;;
  logs)
    check_dependencies
    if [[ "${FOLLOW}" == true ]]; then
      compose logs --follow --tail 100
    else
      compose logs --tail 100
    fi
    ;;
  health)
    require_command curl
    health
    ;;
  doctor)
    doctor
    ;;
  models)
    check_ollama
    ollama_cli list
    ;;
  pull-models)
    pull_models
    ;;
  test)
    check_dependencies
    require_command bash
    bash -n "${ROOT_DIR}/deepwiki.sh"
    mkdir -p "${RUNTIME_DIR}"

    original_network_mode="${EFFECTIVE_NETWORK_MODE}"
    EFFECTIVE_NETWORK_MODE="bridge"
    compose config --quiet
    EFFECTIVE_NETWORK_MODE="host"
    compose config --quiet
    EFFECTIVE_NETWORK_MODE="${original_network_mode}"

    docker image inspect deepwiki-open:ollama >/dev/null 2>&1 ||
      die "Falta la imagen. Ejecuta './deepwiki.sh setup' primero."
    docker run --rm \
      --volume "${ROOT_DIR}:/workspace" \
      --workdir /workspace \
      deepwiki-open:ollama \
      python -m pytest -q \
        test/test_deepwiki_config.py \
        test/test_extract_repo_name.py \
        test/test_ollama_batch.py
    info "Pruebas reproducibles superadas"
    ;;
  config)
    show_config
    ;;
esac
