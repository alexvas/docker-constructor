# syntax=docker/dockerfile:1
# check=skip=InvalidDefaultArgInFrom
# BuildKit cache boundaries:
#   base -> toolchain -> pi-tools and base -> openspec-tools
# Version arguments are scoped to the stage that consumes them.

ARG DEV_UID=1000
ARG DEV_GID=1000

# -----------------------------------------------------------------------------
# Base image — resolved by the versioning resolver (NODE_BASE_IMAGE = <registry>/<repository>:<tag>@<digest>)
# -----------------------------------------------------------------------------
ARG NODE_BASE_IMAGE
FROM ${NODE_BASE_IMAGE} AS base

ARG DEV_UID
ARG DEV_GID

# Optional corporate network build arguments, supplied via --build-arg and
# redeclared in each stage that performs network operations.  They are never
# converted to persistent image ENV metadata.
ARG PI_CORPORATE_PROXY_URL
ARG PI_CORPORATE_NO_PROXY
ARG CORPORATE_TRUST_ENABLED
ARG PI_CORPORATE_CA_PATH

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/home/dev \
    RUSTUP_HOME=/home/dev/.rustup \
    CARGO_HOME=/home/dev/.cargo \
    NPM_CONFIG_PREFIX=/home/dev/.npm-global \
    PATH="/opt/pi/bin:/opt/openspec/bin:/home/dev/.npm-global/bin:/home/dev/.local/bin:/home/dev/.cargo/bin:/usr/local/bin:${PATH}" \
    LANG=ru_RU.UTF-8 \
    LC_ALL=ru_RU.UTF-8

# Optional corporate trust replacement: enabled only by the explicit
# CORPORATE_TRUST_ENABLED build argument, never by bundle-file presence alone,
# so a stale .docker-local/corporate-ca-bundle.crt cannot silently change trust.
# The .docker-local directory is always present in the build context (tracked
# .gitkeep), so this COPY never fails.
COPY .docker-local/ /tmp/corporate-ca/
COPY docker/validate-corporate-bundle.sh /tmp/validate-corporate-bundle.sh
RUN set -eux; \
    if [ "${CORPORATE_TRUST_ENABLED:-}" = "true" ]; then \
        sh /tmp/validate-corporate-bundle.sh /tmp/corporate-ca/corporate-ca-bundle.crt; \
        mkdir -p /etc/ssl/certs; \
        cp /tmp/corporate-ca/corporate-ca-bundle.crt /etc/ssl/certs/ca-certificates.crt; \
    fi

COPY docker/corp-network-env.sh /tmp/corp-network-env.sh
RUN --mount=type=cache,id=apt-cache-trixie,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=apt-lists-trixie,target=/var/lib/apt/lists,sharing=locked \
    . /tmp/corp-network-env.sh \
    && rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf 'Binary::apt::APT::Keep-Downloaded-Packages "true";\n' > /etc/apt/apt.conf.d/keep-cache \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       git vim less bat curl wget ca-certificates jq ripgrep locales \
       openssh-client gh rpm build-essential pkg-config gosu socat bash zsh util-linux \
    && ln -sf /usr/bin/batcat /usr/local/bin/bat \
    && sed -i 's/# ru_RU.UTF-8 UTF-8/ru_RU.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen ru_RU.UTF-8 \
    && apt-get autoclean

# Re-apply the corporate bundle after package installation: the ca-certificates
# package regenerates /etc/ssl/certs/ca-certificates.crt from distro roots, so
# the final image must replace it again to preserve complete-replacement
# semantics.
RUN set -eux; \
    if [ "${CORPORATE_TRUST_ENABLED:-}" = "true" ]; then \
        cp /tmp/corporate-ca/corporate-ca-bundle.crt /etc/ssl/certs/ca-certificates.crt; \
    fi; \
    rm -rf /tmp/corporate-ca

COPY docker/setup-dev-user.sh /tmp/setup-dev-user.sh
RUN chmod +x /tmp/setup-dev-user.sh \
    && DEV_UID="${DEV_UID}" DEV_GID="${DEV_GID}" /tmp/setup-dev-user.sh \
    && rm -f /tmp/setup-dev-user.sh

# -----------------------------------------------------------------------------
# Independently pinned prebuilt Rust tools
# -----------------------------------------------------------------------------
FROM base AS rtk-prebuilt

# Corporate network build arguments supplied via --build-arg; never persisted
# as ENV.
ARG PI_CORPORATE_PROXY_URL
ARG PI_CORPORATE_NO_PROXY
ARG CORPORATE_TRUST_ENABLED
ARG PI_CORPORATE_CA_PATH

ARG RTK_VERSION
ARG RTK_SHA256
# hadolint ignore=DL3022
COPY --from=constructor-artifacts --chmod=0444 rtk.deb /tmp/rtk.deb
RUN ACTUAL=$(sha256sum /tmp/rtk.deb | cut -d' ' -f1) \
    && if [ "$ACTUAL" != "${RTK_SHA256}" ]; then echo "SHA256 mismatch: expected ${RTK_SHA256}, got $ACTUAL" >&2; exit 1; fi \
    && dpkg-deb -x /tmp/rtk.deb /tmp/rtk-extract \
    && install -m 755 /tmp/rtk-extract/usr/bin/rtk /usr/local/bin/rtk \
    && rm -rf /tmp/rtk.deb /tmp/rtk-extract

FROM base AS fd-prebuilt

# Corporate network build arguments supplied via --build-arg; never persisted
# as ENV.
ARG PI_CORPORATE_PROXY_URL
ARG PI_CORPORATE_NO_PROXY
ARG CORPORATE_TRUST_ENABLED
ARG PI_CORPORATE_CA_PATH

ARG FD_VERSION
ARG FD_SHA256
# hadolint ignore=DL3022
COPY --from=constructor-artifacts --chmod=0444 fd.deb /tmp/fd.deb
RUN ACTUAL=$(sha256sum /tmp/fd.deb | cut -d' ' -f1) \
    && if [ "$ACTUAL" != "${FD_SHA256}" ]; then echo "SHA256 mismatch: expected ${FD_SHA256}, got $ACTUAL" >&2; exit 1; fi \
    && dpkg-deb -x /tmp/fd.deb /tmp/fd-extract \
    && install -m 755 /tmp/fd-extract/usr/bin/fd /usr/local/bin/fd \
    && rm -rf /tmp/fd.deb /tmp/fd-extract

# -----------------------------------------------------------------------------
# Builder-only OS packages and stable toolchain setup
# -----------------------------------------------------------------------------
FROM base AS toolchain

# Corporate network build arguments supplied via --build-arg; never persisted
# as ENV.
ARG PI_CORPORATE_PROXY_URL
ARG PI_CORPORATE_NO_PROXY
ARG CORPORATE_TRUST_ENABLED
ARG PI_CORPORATE_CA_PATH

RUN --mount=type=cache,id=apt-cache-trixie,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=apt-lists-trixie,target=/var/lib/apt/lists,sharing=locked \
    . /tmp/corp-network-env.sh \
    && apt-get update \
    && apt-get install -y --no-install-recommends privoxy xz-utils netcat-openbsd iproute2 \
    && apt-get autoclean

USER dev
WORKDIR /home/dev
RUN mkdir -p /home/dev/.cargo /home/dev/.rustup /home/dev/.cache/uv /home/dev/mcp

ARG RUST_VERSION
ARG RUST_PROFILE
ARG RUST_COMPONENTS
ARG RUSTUP_SHA256
# hadolint ignore=DL3022
COPY --from=constructor-artifacts --chown=dev:dev --chmod=0555 rustup-init /tmp/rustup-init
# The single-quoted program intentionally defers expansion to the inner bash.
# hadolint ignore=SC2016
RUN --mount=type=cache,id=cargo-registry-${DEV_UID}-${DEV_GID},target=/home/dev/.cargo/registry,uid=${DEV_UID},gid=${DEV_GID} \
    --mount=type=cache,id=cargo-git-${DEV_UID}-${DEV_GID},target=/home/dev/.cargo/git,uid=${DEV_UID},gid=${DEV_GID} \
    --mount=type=cache,id=rustup-downloads-${DEV_UID}-${DEV_GID},target=/home/dev/.rustup/downloads,uid=${DEV_UID},gid=${DEV_GID} \
    env HOME=/home/dev CARGO_HOME=/home/dev/.cargo RUSTUP_HOME=/home/dev/.rustup \
    bash -euo pipefail -c ' \
        printf "%s  %s\n" "${RUSTUP_SHA256}" /tmp/rustup-init | sha256sum -c - \
        && /tmp/rustup-init -y --profile "${RUST_PROFILE}" --default-toolchain "${RUST_VERSION}" \
        && rustup component add ${RUST_COMPONENTS} \
        && ACTUAL_RUSTC="$(rustc --version | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -1)" \
        && test "${ACTUAL_RUSTC}" = "${RUST_VERSION}" \
        && ACTUAL_CARGO="$(cargo --version | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -1)" \
        && test "${ACTUAL_CARGO}" = "${RUST_VERSION}" \
        && rustfmt --version \
        && cargo clippy --version \
        && rm -f /tmp/rustup-init'

ARG UV_VERSION
ARG UV_SHA256
# hadolint ignore=DL3022
COPY --from=constructor-artifacts --chown=dev:dev --chmod=0444 uv.tar.gz /tmp/uv.tar.gz
RUN --mount=type=cache,id=uv-downloads-${DEV_UID}-${DEV_GID},target=/home/dev/.cache/uv,uid=${DEV_UID},gid=${DEV_GID} \
    bash -euo pipefail -c ' \
        printf "%s  %s\n" "${UV_SHA256}" /tmp/uv.tar.gz | sha256sum -c - \
        && tar xzf /tmp/uv.tar.gz -C /tmp \
        && ACTUAL_UV="$(/tmp/uv-*/uv --version | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -1)" \
        && test "${ACTUAL_UV}" = "${UV_VERSION}" \
        && install -D -m 755 /tmp/uv-*/uv "${HOME}/.local/bin/uv" \
        && ACTUAL_UV2="$(uv --version | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -1)" \
        && test "${ACTUAL_UV2}" = "${UV_VERSION}" \
        && rm -rf /tmp/uv.tar.gz /tmp/uv-*'

ARG PYTHON_VERSION
COPY --chown=dev:dev docker/setup-python.sh /home/dev/setup-python.sh
RUN --mount=type=cache,id=uv-downloads-${DEV_UID}-${DEV_GID},target=/home/dev/.cache/uv,uid=${DEV_UID},gid=${DEV_GID} \
    . /tmp/corp-network-env.sh \
    && chmod +x /home/dev/setup-python.sh \
    && PYTHON_VERSION="${PYTHON_VERSION}" /home/dev/setup-python.sh

ARG TY_VERSION
RUN --mount=type=cache,id=uv-downloads-${DEV_UID}-${DEV_GID},target=/home/dev/.cache/uv,uid=${DEV_UID},gid=${DEV_GID} \
    . /tmp/corp-network-env.sh \
    && uv tool install --python "${PYTHON_VERSION}" "ty==${TY_VERSION}"

COPY --chown=dev:dev docker/mcp /home/dev/mcp
COPY --chown=dev:dev docker/setup-mcp-yarn.sh /home/dev/setup-mcp-yarn.sh
RUN bash /home/dev/setup-mcp-yarn.sh

# -----------------------------------------------------------------------------
# Independently versioned Node tool prefixes
# -----------------------------------------------------------------------------
FROM base AS pi-tools

# Corporate network build arguments supplied via --build-arg; never persisted
# as ENV.
ARG PI_CORPORATE_PROXY_URL
ARG PI_CORPORATE_NO_PROXY
ARG CORPORATE_TRUST_ENABLED
ARG PI_CORPORATE_CA_PATH

# Post-materialization attestation values, matched to the named-context inputs
# before the Pi tree and both evidence sets are copied and verified here.
ARG PI_VERSION
ARG PI_ASSEMBLED_OUTPUT_IDENTITY
ARG PI_TREE_DIGEST
ARG PI_ASSEMBLER_EVIDENCE_DIGEST
ARG PI_ASSEMBLER_EVIDENCE_BYTES_DIGEST
ARG PI_LAUNCHER_EVIDENCE_DIGEST

USER root
# hadolint ignore=DL3022
COPY --from=constructor-artifacts derived-environments/pi/opt/pi /opt/pi
# hadolint ignore=DL3022
COPY --from=constructor-artifacts --chmod=0444 derived-environments/pi/pi-assembler-evidence.json /tmp/pi-assembler-evidence.json
# hadolint ignore=DL3022
COPY --from=constructor-artifacts --chmod=0444 derived-environments/pi/pi-launcher-evidence.json /tmp/pi-launcher-evidence.json
COPY docker/verify-pi.mjs /tmp/verify-pi.mjs
RUN node /tmp/verify-pi.mjs \
    && rm -f /tmp/verify-pi.mjs /tmp/pi-assembler-evidence.json /tmp/pi-launcher-evidence.json
USER dev

FROM base AS openspec-tools

# Corporate network build arguments supplied via --build-arg; never persisted
# as ENV.
ARG PI_CORPORATE_PROXY_URL
ARG PI_CORPORATE_NO_PROXY
ARG CORPORATE_TRUST_ENABLED
ARG PI_CORPORATE_CA_PATH

ARG OPENSPEC_VERSION
USER root
RUN mkdir -p /opt/openspec && chown -R dev:dev /opt/openspec
USER dev
RUN --mount=type=cache,id=npm-openspec-${DEV_UID}-${DEV_GID},target=/home/dev/.npm,uid=${DEV_UID},gid=${DEV_GID} \
    . /tmp/corp-network-env.sh \
    && npm_config_cache=/home/dev/.npm npm install --global --prefix /opt/openspec "@fission-ai/openspec@${OPENSPEC_VERSION}"

# -----------------------------------------------------------------------------
# Runtime assembly; no builder-only packages or cache mounts are copied
# -----------------------------------------------------------------------------
FROM base AS runtime

# Corporate network build arguments supplied via --build-arg; never persisted
# as ENV.
ARG PI_CORPORATE_PROXY_URL
ARG PI_CORPORATE_NO_PROXY
ARG CORPORATE_TRUST_ENABLED
ARG PI_CORPORATE_CA_PATH

ARG OH_MY_ZSH_VERSION

COPY --from=pi-tools /opt/pi /opt/pi
# --chown applies to every copied descendant, including when DEV_UID/GID are
# customized in the base stage.  Do not replace this with a whole-home rewrite.
COPY --chown=dev:dev --from=toolchain /home/dev/.local /home/dev/.local
COPY --chown=dev:dev --from=toolchain /home/dev/.rustup /home/dev/.rustup
COPY --chown=dev:dev --from=toolchain /home/dev/.cargo/bin /home/dev/.cargo/bin
COPY --chown=dev:dev --from=toolchain /home/dev/mcp /home/dev/mcp

RUN ln -sf /opt/pi/bin/pi /usr/local/bin/pi \
    && install -d -o dev -g dev /home/dev/work \
    && install -d -o dev -g dev /home/dev/.npm-global \
    && install -d -o dev -g dev /home/dev/.npm-global/bin

COPY --from=rtk-prebuilt /usr/local/bin/rtk /usr/local/bin/rtk
COPY --from=fd-prebuilt /usr/local/bin/fd /usr/local/bin/fd

COPY docker/zsh/zshrc.fragment /tmp/zshrc.fragment
COPY docker/setup-zsh.sh /tmp/setup-zsh.sh
RUN . /tmp/corp-network-env.sh \
    && chmod +x /tmp/setup-zsh.sh \
    && runuser -u dev -- env HOME=/home/dev OH_MY_ZSH_VERSION="${OH_MY_ZSH_VERSION}" /tmp/setup-zsh.sh \
    && rm -f /tmp/setup-zsh.sh /tmp/zshrc.fragment

# Keep OpenSpec version changes after unrelated home and zsh setup.
COPY --from=openspec-tools /opt/openspec /opt/openspec
RUN ln -sf /opt/openspec/bin/openspec /usr/local/bin/openspec

# Installer module for runtime extension management.
COPY docker/versioning/ /usr/local/lib/pi-cli/docker/versioning/
COPY docker/runtime_installer.py /usr/local/lib/pi-cli/docker/runtime_installer.py
RUN chown -R root:root /usr/local/lib/pi-cli \
    && chmod -R a+rX /usr/local/lib/pi-cli

COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Keep the image root by default: entrypoint.sh repairs bind-mount ownership and drops to dev.
WORKDIR /home/dev
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["zsh"]
