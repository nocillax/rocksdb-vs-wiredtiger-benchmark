#!/usr/bin/env bash
set -euo pipefail

# 1. Ensure sudo privileges early
sudo -v

# 2. Update package databases
echo "==> Updating package databases..."
sudo pacman -Syu --noconfirm

# 3. Define package groups and packages separately
PACKAGE_GROUPS=(
    base-devel
)

PACKAGES=(
    cmake
    git
    pkgconf
    openssl
    zlib
    snappy
    lz4
    zstd
    nvme-cli
    python
    python-numpy
    python-pandas
    python-matplotlib
    python-tqdm
)

echo "==> Installing package groups..."
for grp in "${PACKAGE_GROUPS[@]}"; do
    if ! pacman -Qg "$grp" &>/dev/null; then
        echo "Installing group: $grp..."
        sudo pacman -S --needed --noconfirm "$grp"
    fi
done

echo "==> Installing packages in batch..."
sudo pacman -S --needed --noconfirm "${PACKAGES[@]}" || true

echo "==> Verifying each required package individually..."
FAILED_PACKAGES=()

for pkg in "${PACKAGES[@]}"; do
    if ! pacman -Qi "$pkg" &>/dev/null; then
        echo "Missing: $pkg. Attempting direct installation..."
        if ! sudo pacman -S --needed --noconfirm "$pkg"; then
            FAILED_PACKAGES+=("$pkg")
        fi
    fi
done

# 4. Strict check to guarantee all dependencies exist before continuing
if [ ${#FAILED_PACKAGES[@]} -gt 0 ]; then
    echo "ERROR: The following packages failed to install:" >&2
    for failed in "${FAILED_PACKAGES[@]}"; do
        echo "  - $failed" >&2
    done
    exit 1
fi

echo "Environment setup complete and all dependencies verified."