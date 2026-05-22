# north9-runtime — general-purpose image for AI agent sandboxes
# Pre-installed: Python, Node, git, curl, make, gcc, jq, ripgrep
#
# Build: docker build -t north9/north9-runtime .
# Use:   north9 --image north9/north9-runtime

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core utils
    curl wget git make jq \
    # C/C++ build tools
    gcc g++ build-essential \
    # Python
    python3.11 python3.11-venv python3-pip \
    # Node.js (via nodesource)
    ca-certificates gnupg \
    # Shell tools
    ripgrep fd-find tree \
    && rm -rf /var/lib/apt/lists/*

# Node.js LTS
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Python aliases
RUN ln -sf /usr/bin/python3.11 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python3 \
    && python3 -m pip install --upgrade pip --quiet

# npm globals: pnpm
RUN npm install -g pnpm --quiet

WORKDIR /workspace
