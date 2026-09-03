#!/bin/sh
# Put `igdl` on your PATH and check the one dependency.
set -e

root="$(cd "$(dirname "$0")" && pwd)"
bindir="${HOME}/.local/bin"

mkdir -p "$bindir"
for launcher in igdl igbulk-mcp; do
  chmod +x "$root/$launcher"
  ln -sf "$root/$launcher" "$bindir/$launcher"
  echo "Linked $bindir/$launcher -> $root/$launcher"
done

if ! command -v yt-dlp >/dev/null 2>&1; then
  echo
  echo "yt-dlp is missing — install it with:  brew install yt-dlp"
fi

case ":$PATH:" in
  *":$bindir:"*) ;;
  *)
    echo
    echo "$bindir is not on your PATH. Add this to ~/.zshrc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

echo
echo "Done. Try:  igdl --serve"
