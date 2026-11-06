# syntax=docker/dockerfile:1.7
# Mount File's container image intentionally contains the CLI only; Qt and
# other GUI dependencies are not installed.
FROM python:3.12-slim

ARG PYTSK3_VERSION=20260715
ARG INSTALL_LEGACY_TSK=0
ARG INSTALL_FORENSIC_TOOLS=0

ENV HOME=/home/mountfile \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/mountfile \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The default image uses the native readers and needs no apt or pip packages.
# Opt in only when command-line forensic utilities
# are useful in the same container:
#   INSTALL_FORENSIC_TOOLS=1 adds the optional read-only triage toolchain:
#   - sleuthkit, testdisk and binutils: filesystem/recovery inspection helpers
#   - dosfstools, mtools and e2fsprogs: FAT/ext image inspection and fsck tools
#   - ntfs-3g, squashfs-tools and mtd-utils: filesystem/flash image utilities
#   - cryptsetup and qemu-utils: bounded container and image inspection helpers
#   - file, binwalk and yara: identification, signature and rule scanning tools
# Example: docker build --build-arg INSTALL_FORENSIC_TOOLS=1 -t mountfile:forensics .
RUN set -eux; \
    case "${INSTALL_FORENSIC_TOOLS}" in \
        0) ;; \
        1) \
            apt-get update; \
            apt-get install --yes --no-install-recommends \
                binutils binwalk cryptsetup dosfstools e2fsprogs file \
                mtd-utils mtools ntfs-3g qemu-utils sleuthkit squashfs-tools \
                testdisk yara; \
            rm -rf /var/lib/apt/lists/*; \
            ;; \
        *) \
            echo "INSTALL_FORENSIC_TOOLS must be 0 or 1" >&2; \
            exit 2; \
            ;; \
    esac

# Legacy fallback is opt-in, just like the package's legacy-tsk extra. Require
# a wheel rather than silently installing a compiler toolchain in a CLI image.
# Pinning the version keeps deliberately enabled builds repeatable.
RUN --mount=type=cache,target=/root/.cache/pip \
    set -eux; \
    case "${INSTALL_LEGACY_TSK}" in \
        0) ;; \
        1) python -m pip install --only-binary=:all: "pytsk3==${PYTSK3_VERSION}" ;; \
        *) echo "INSTALL_LEGACY_TSK must be 0 or 1" >&2; exit 2 ;; \
    esac

WORKDIR /opt/mountfile
COPY mountfile/ ./mountfile/
COPY mountfile_dfir_tools.py ./

# The source tree is used directly so installing project metadata cannot pull
# in GUI-only packages. mountfile.__main__ is the supported CLI module and is
# kept as the container's executable entrypoint.
RUN addgroup --system --gid 10001 mountfile \
    && adduser --system --uid 10001 --ingroup mountfile \
        --home /home/mountfile --shell /usr/sbin/nologin mountfile \
    && mkdir -p /home/mountfile /evidence /output \
    && chown -R 10001:10001 /opt/mountfile /home/mountfile /evidence /output

USER 10001:10001

# Keep evidence read-only at runtime, for example:
# docker run --rm --read-only --cap-drop=ALL \
#   --security-opt no-new-privileges \
#   --mount type=bind,src="$PWD/evidence",dst=/evidence,ro \
#   --mount type=bind,src="$PWD/output",dst=/output \
#   mountfile:local info /evidence/dump.img
# Write commands should target an explicit writable copy, never the evidence
# mount. The default compose example below enforces this distinction.
ENTRYPOINT ["python", "-m", "mountfile"]
