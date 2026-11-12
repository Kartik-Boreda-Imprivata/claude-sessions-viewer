#!/usr/bin/env bash
# Installs the `csr` command (macOS/Linux). Defaults to --tui unless the
# args already request a non-interactive mode (--tui/--detail/--compare).
set -euo pipefail

REPO="Kartik-Boreda-Imprivata/claude-sessions-viewer"
RAW_BASE="https://raw.githubusercontent.com/${REPO}/main"
LIB_DIR="${HOME}/.local/share/claude-sessions-viewer"
BIN_DIR="${HOME}/.local/bin"

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

mkdir -p "$LIB_DIR" "$BIN_DIR"
curl -fsSL "${RAW_BASE}/claude_session_report.py" -o "${LIB_DIR}/claude_session_report.py"
chmod +x "${LIB_DIR}/claude_session_report.py"

cat > "${BIN_DIR}/csr" <<EOF
#!/usr/bin/env bash
args=("\$@")
want_tui=1
for a in "\$@"; do
  case "\$a" in
    --tui|--detail|--compare) want_tui=0 ;;
  esac
done
if [ "\$want_tui" -eq 1 ] && [ -t 0 ] && [ -t 1 ]; then
  args+=("--tui")
fi
exec python3 "${LIB_DIR}/claude_session_report.py" "\${args[@]}"
EOF
chmod +x "${BIN_DIR}/csr"

echo "Installed csr to ${BIN_DIR}/csr"
case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) echo "Add it to your PATH: echo 'export PATH=\"${BIN_DIR}:\$PATH\"' >> ~/.bashrc  # or ~/.zshrc" ;;
esac
